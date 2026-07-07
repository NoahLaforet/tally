"""Transactions API: paginated list with filters, inline recategorize, export.

The list powers the transactions view in the frontend. Rows carry origin and
transfer flags so the UI can show source and transfer badges. A recategorize
upserts the LearnedCategory table and fans the new category out to every other
unlocked row with the same merchant, which is how one manual fix teaches the
categorizer.

Money rule: amounts are integer cents in the database. Dollars appear only in
the JSON output, computed as round(cents / 100, 2) at this boundary. This API
never accepts a money amount as input.

The router is defined here and registered in app.main by the app wiring, never
in this module.
"""

from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlmodel import Session, func, or_, select

from .auth import require_user
from .db import get_session
from .events import hub
from .models import ReimbursementRule, Account, LearnedCategory, Transaction

# require_user is defense in depth; the global auth gate middleware also
# covers every /api route when auth is enabled.
router = APIRouter(prefix="/api/transactions", tags=["transactions"],
                   dependencies=[Depends(require_user)])

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Every column a row dict carries. Also the CSV header, in this order.
COLUMNS = [
    "uid", "date", "amount", "amount_cents", "merchant", "description",
    "category", "category_source", "account_id", "account",
    "is_transfer", "origin", "user_locked", "reimbursement", "note",
]


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so a search for a literal % or _ works."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filters(q: str | None = None, account_id: int | None = None,
             category: str | None = None, origin: str | None = None,
             date_from: date | None = None, date_to: date | None = None) -> dict:
    """Shared filter query params for the list and both exports."""
    return {"q": q, "account_id": account_id, "category": category,
            "origin": origin, "date_from": date_from, "date_to": date_to}


def _conditions(f: dict) -> list:
    conds = []
    if f.get("q"):
        needle = "%" + _escape_like(f["q"].lower()) + "%"
        conds.append(or_(
            func.lower(Transaction.norm_merchant).like(needle, escape="\\"),
            func.lower(Transaction.raw_description).like(needle, escape="\\"),
            func.lower(Transaction.note).like(needle, escape="\\"),
        ))
    if f.get("account_id") is not None:
        conds.append(Transaction.account_id == f["account_id"])
    if f.get("category"):
        conds.append(Transaction.category == f["category"])
    if f.get("origin"):
        conds.append(Transaction.origin == f["origin"])
    if f.get("date_from"):
        conds.append(Transaction.posted_date >= f["date_from"])
    if f.get("date_to"):
        conds.append(Transaction.posted_date <= f["date_to"])
    return conds


def _row(t: Transaction, account_names: dict[int, str]) -> dict:
    return {
        "uid": t.txn_uid,
        "date": t.posted_date.isoformat(),
        "amount": round(t.amount_cents / 100, 2),
        "amount_cents": t.amount_cents,
        "merchant": t.norm_merchant,
        "description": t.raw_description,
        "category": t.category,
        "category_source": t.category_source,
        "account_id": t.account_id,
        "account": account_names.get(t.account_id),
        "is_transfer": t.is_transfer,
        "origin": t.origin,
        "user_locked": t.user_locked,
        "reimbursement": t.reimbursement,
        "note": t.note,
    }


def _fetch_rows(session: Session, f: dict, offset: int | None = None,
                limit: int | None = None) -> list[dict]:
    stmt = select(Transaction)
    for c in _conditions(f):
        stmt = stmt.where(c)
    stmt = stmt.order_by(Transaction.posted_date.desc(), Transaction.txn_uid)
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    txns = session.exec(stmt).all()
    # One query for all account names instead of one per row.
    names = {a.id: a.name for a in session.exec(select(Account)).all()}
    return [_row(t, names) for t in txns]


def _count(session: Session, f: dict) -> int:
    stmt = select(func.count()).select_from(Transaction)
    for c in _conditions(f):
        stmt = stmt.where(c)
    return session.exec(stmt).one()


def list_transactions(session: Session, f: dict, page: int = 1,
                      page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """One page of filtered transactions plus the total match count."""
    page = max(1, page)
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    total = _count(session, f)
    rows = _fetch_rows(session, f, offset=(page - 1) * page_size, limit=page_size)
    return {"total": total, "page": page, "page_size": page_size, "rows": rows}


# 'mine' = reviewed and kept as own spending; persists so the review queue
# never asks about that charge again. Only group/thirdparty affect the math.
REIMBURSEMENT_KINDS = {"group", "thirdparty", "mine"}


def apply_patch(session: Session, txn_uid: str, category: str | None,
                user_locked: bool | None,
                reimbursement: str | None = None,
                clear_reimbursement: bool = False,
                apply_to_merchant: bool = False,
                note: str | None = None) -> dict | None:
    """Apply an inline edit to one transaction. Returns None on unknown uid.

    Setting a category is a manual override: it locks the row (unless the
    caller explicitly passes user_locked=false), records the merchant mapping
    in LearnedCategory, and re-labels every other unlocked row with the same
    merchant as learned. Passing only user_locked toggles the lock.
    """
    txn = session.get(Transaction, txn_uid)
    if txn is None:
        return None

    updated_others = 0
    if category is not None:
        cat = category.strip()
        if not cat:
            raise ValueError("category cannot be empty")
        txn.category = cat
        txn.category_source = "manual"
        txn.user_locked = user_locked is not False
        lc = session.get(LearnedCategory, txn.norm_merchant)
        if lc is None:
            session.add(LearnedCategory(norm_merchant=txn.norm_merchant,
                                        category=cat))
        else:
            lc.category = cat
            session.add(lc)
        others = session.exec(
            select(Transaction)
            .where(Transaction.norm_merchant == txn.norm_merchant)
            .where(Transaction.txn_uid != txn.txn_uid)
            .where(Transaction.user_locked == False)  # noqa: E712
        ).all()
        for other in others:
            other.category = cat
            other.category_source = "learned"
            session.add(other)
        updated_others = len(others)
    elif user_locked is not None:
        txn.user_locked = user_locked

    if clear_reimbursement:
        txn.reimbursement = None
        if apply_to_merchant:
            # Standing order revoked: forget the rule and unmark every
            # sibling charge from this merchant.
            rule = session.get(ReimbursementRule, txn.norm_merchant)
            if rule is not None:
                session.delete(rule)
            siblings = session.exec(
                select(Transaction)
                .where(Transaction.norm_merchant == txn.norm_merchant)
                .where(Transaction.txn_uid != txn.txn_uid)
                .where(Transaction.reimbursement != None)  # noqa: E711
            ).all()
            for other in siblings:
                other.reimbursement = None
                session.add(other)
            updated_others += len(siblings)
    elif reimbursement is not None:
        kind = reimbursement.strip().lower()
        if kind not in REIMBURSEMENT_KINDS:
            raise ValueError("reimbursement must be 'group' or 'thirdparty'")
        txn.reimbursement = kind
        if apply_to_merchant and kind != "mine":
            # Default behavior: marking once creates a standing order, so
            # next month's rent excludes itself.
            rule = session.get(ReimbursementRule, txn.norm_merchant)
            if rule is None:
                session.add(ReimbursementRule(norm_merchant=txn.norm_merchant,
                                              kind=kind))
            else:
                rule.kind = kind
                session.add(rule)
            siblings = session.exec(
                select(Transaction)
                .where(Transaction.norm_merchant == txn.norm_merchant)
                .where(Transaction.txn_uid != txn.txn_uid)
                .where(Transaction.amount_cents < 0)
                .where(Transaction.reimbursement == None)  # noqa: E711
            ).all()
            for other in siblings:
                other.reimbursement = kind
                session.add(other)
            updated_others += len(siblings)

    if note is not None:
        # A note edit is just a tag. It never locks, never teaches the
        # learned table, never fans out. Empty string clears to NULL.
        txn.note = note.strip() or None

    session.add(txn)
    session.commit()
    return {"ok": True, "updated_others": updated_others}


def apply_bulk(session: Session, uids: list[str], category: str | None = None,
               reimbursement: str | None = None,
               clear_reimbursement: bool = False) -> dict:
    """Apply one edit to a set of selected transactions in a single commit.

    Bulk means exactly the rows you picked: recategorizing still teaches the
    LearnedCategory table per merchant (so future imports benefit) but does NOT
    fan out to unselected siblings, and marking a repayment here does NOT create
    a standing merchant rule (that stays a deliberate single-row action). Returns
    how many rows changed and any uids that did not exist.
    """
    uids = list(dict.fromkeys(uids))[:1000]  # dedupe, and cap defensively
    if not uids:
        return {"ok": True, "updated": 0, "missing": []}
    cat = None
    if category is not None:
        cat = category.strip()
        if not cat:
            raise ValueError("category cannot be empty")
    kind = None
    if reimbursement is not None and not clear_reimbursement:
        kind = reimbursement.strip().lower()
        if kind not in REIMBURSEMENT_KINDS:
            raise ValueError("reimbursement must be 'group', 'thirdparty', or 'mine'")

    txns = session.exec(
        select(Transaction).where(Transaction.txn_uid.in_(uids))).all()
    for t in txns:
        if cat is not None:
            t.category = cat
            t.category_source = "manual"
            t.user_locked = True
            lc = session.get(LearnedCategory, t.norm_merchant)
            if lc is None:
                session.add(LearnedCategory(norm_merchant=t.norm_merchant,
                                            category=cat))
            else:
                lc.category = cat
                session.add(lc)
        if clear_reimbursement:
            t.reimbursement = None
        elif kind is not None:
            t.reimbursement = kind
        session.add(t)
    session.commit()
    found = {t.txn_uid for t in txns}
    return {"ok": True, "updated": len(txns),
            "missing": [u for u in uids if u not in found]}


class BulkBody(BaseModel):
    uids: list[str]
    category: str | None = None
    reimbursement: str | None = None
    clear_reimbursement: bool = False


class TxnPatch(BaseModel):
    category: str | None = None
    user_locked: bool | None = None
    # 'group' or 'thirdparty' marks money that came back; null clears when
    # clear_reimbursement is set (a bare null must not clear by accident).
    reimbursement: str | None = None
    clear_reimbursement: bool = False
    # True (the UI default) makes the mark a standing order for the merchant.
    apply_to_merchant: bool = False
    # Free-text tag. None = unchanged; empty string = clear (stored as NULL).
    note: str | None = None


@router.get("")
def api_list(page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
             filters: dict = Depends(_filters),
             session: Session = Depends(get_session)) -> dict:
    return list_transactions(session, filters, page=page, page_size=page_size)


@router.patch("/{txn_uid}")
def api_patch(txn_uid: str, body: TxnPatch,
              session: Session = Depends(get_session)) -> dict:
    try:
        result = apply_patch(session, txn_uid, body.category, body.user_locked,
                             body.reimbursement, body.clear_reimbursement,
                             body.apply_to_merchant, body.note)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    if result is None:
        raise HTTPException(404, "transaction not found")
    hub.publish("transactions:updated")
    return result


@router.post("/bulk")
def api_bulk(body: BulkBody,
             session: Session = Depends(get_session)) -> dict:
    try:
        result = apply_bulk(session, body.uids, body.category,
                            body.reimbursement, body.clear_reimbursement)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    hub.publish("transactions:updated")
    return result


@router.get("/export.csv")
def api_export_csv(filters: dict = Depends(_filters),
                   session: Session = Depends(get_session)) -> Response:
    rows = _fetch_rows(session, filters)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition":
                 'attachment; filename="tally-transactions.csv"'},
    )


@router.get("/export.json")
def api_export_json(filters: dict = Depends(_filters),
                    session: Session = Depends(get_session)) -> list[dict]:
    return _fetch_rows(session, filters)
