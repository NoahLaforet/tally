"""Reimbursement detection: fronted money that came back.

Finds outflows that look repaid: a sizable charge followed within a couple of
weeks by inflows that add back to exactly the same amount (one Zelle for the
rent, or several Venmo legs for a group dinner). These are SUGGESTIONS only;
the user confirms each one in the transactions view, which sets
Transaction.reimbursement ('group' or 'thirdparty') and removes the charge
from every spend figure.

All matching is exact integer cents. An inflow is only offered once across
the suggestion set, transfers and already-marked rows never participate, and
inflows that are themselves obvious non-repayments (payroll, interest) are
screened by a small lexicon.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import combinations

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from .auth import require_user
from .db import engine
from .models import Account, Transaction

router = APIRouter(prefix="/api/reimbursements", tags=["reimbursements"],
                   dependencies=[Depends(require_user)])

MIN_OUTFLOW_CENTS = 15_000     # only sizable charges are worth suggesting
WINDOW_DAYS = 14               # repayment must land within two weeks
MAX_LEGS = 3                   # combine at most this many inflows

# Inflows that are never repayments.
_NOT_REPAYMENT = ("payroll", "direct dep", "interest", "dividend", "refund",
                  "cash back", "daily cash", "tax ref")


def find_suggestions(session: Session, min_cents: int = MIN_OUTFLOW_CENTS,
                     window_days: int = WINDOW_DAYS) -> list[dict]:
    txns = session.exec(
        select(Transaction).where(Transaction.is_transfer == False)  # noqa: E712
        .where(Transaction.reimbursement == None)  # noqa: E711
    ).all()
    accounts = {a.id: a.name for a in session.exec(select(Account)).all()}

    outflows = sorted((t for t in txns if t.amount_cents <= -min_cents),
                      key=lambda t: t.posted_date, reverse=True)
    inflows = [t for t in txns
               if t.amount_cents > 0
               and not any(k in (t.norm_merchant + " " + t.raw_description).lower()
                           for k in _NOT_REPAYMENT)]

    used: set[str] = set()
    out = []
    for o in outflows:
        target = -o.amount_cents
        lo, hi = o.posted_date, o.posted_date + timedelta(days=window_days)
        pool = [i for i in inflows
                if i.txn_uid not in used and lo <= i.posted_date <= hi
                and i.amount_cents <= target]
        pool.sort(key=lambda i: (i.posted_date, i.txn_uid))
        legs = None
        for i in pool:  # single exact repayment first (the common case)
            if i.amount_cents == target:
                legs = [i]
                break
        if legs is None and len(pool) <= 24:
            for k in (2, MAX_LEGS):
                for combo in combinations(pool, k):
                    if sum(c.amount_cents for c in combo) == target:
                        legs = list(combo)
                        break
                if legs:
                    break
        if not legs:
            continue
        used.update(l.txn_uid for l in legs)
        out.append({
            "uid": o.txn_uid,
            "date": o.posted_date.isoformat(),
            "merchant": o.norm_merchant,
            "description": o.raw_description,
            "amount": round(-o.amount_cents / 100, 2),
            "account": accounts.get(o.account_id, ""),
            "repaid_by": [{
                "uid": l.txn_uid, "date": l.posted_date.isoformat(),
                "merchant": l.norm_merchant,
                "amount": round(l.amount_cents / 100, 2),
                "account": accounts.get(l.account_id, ""),
            } for l in legs],
        })
    return out


@router.get("/suggestions")
def api_suggestions(min_dollars: float = 150.0, window_days: int = WINDOW_DAYS) -> list[dict]:
    min_cents = max(1, round(min_dollars * 100))
    window_days = max(1, min(60, window_days))
    with Session(engine) as s:
        return find_suggestions(s, min_cents=min_cents, window_days=window_days)
