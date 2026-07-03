"""Server-side persistence for everything the dashboard edits.

Replaces the frontend's localStorage as the source of truth for budgets,
income sources, manual accounts, the savings plan, and the net worth series.
Money is signed integer cents in the database. Dollars exist only at the JSON
boundary: incoming dollars become cents with round(float(x) * 100) exactly
once, outgoing cents become dollars with round(cents / 100, 2). APY comes in
as a percent number and is stored in basis points.

The /api/migrate-local endpoint accepts the frontend's localStorage blob
(key 'tally:v2', shape defined by DEFAULTS in frontend/index.html) and maps
it into server rows. It is idempotent; every section upserts by natural key.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import require_user
from .db import engine
from .models import (Account, BalanceSnapshot, Budget, IncomeSource, Setting,
                     Transaction)
from .plaid_link import _snapshot_balance

# Router-level auth on top of the global gate middleware, defense in depth.
router = APIRouter(prefix="/api", tags=["settings"],
                   dependencies=[Depends(require_user)])

SAVINGS_KEY = "savings_plan"


# ---------- boundary conversions ----------

def _dollars(cents: int) -> float:
    return round(cents / 100, 2)


def _cents(dollars) -> int:
    # The single place where incoming dollar amounts become integer cents.
    return round(float(dollars) * 100)


def _bps(percent) -> int:
    # APY arrives as a percent number, 3.4 means 3.40 percent, stored as bps.
    return round(float(percent) * 100)


# ---------- budgets ----------

class BudgetsBody(BaseModel):
    targets: dict[str, float]
    replace: bool = False


def apply_budget_targets(session: Session, targets: dict,
                         replace: bool) -> dict:
    """Upsert a Budget row per given category. With replace, categories not
    present in the payload are deleted. Returns counts."""
    wanted = {str(cat): _cents(val) for cat, val in targets.items()}
    upserted = 0
    deleted = 0
    for cat, cents in wanted.items():
        row = session.get(Budget, cat)
        if row is None:
            row = Budget(category=cat, target_cents=cents)
        else:
            row.target_cents = cents
        session.add(row)
        upserted += 1
    if replace:
        for row in session.exec(select(Budget)).all():
            if row.category not in wanted:
                session.delete(row)
                deleted += 1
    return {"upserted": upserted, "deleted": deleted}


@router.get("/budgets")
def get_budgets() -> list[dict]:
    with Session(engine) as s:
        return [{"category": b.category, "target": _dollars(b.target_cents)}
                for b in s.exec(select(Budget)).all()]


@router.put("/budgets")
def put_budgets(body: BudgetsBody) -> dict:
    with Session(engine) as s:
        counts = apply_budget_targets(s, body.targets, body.replace)
        s.commit()
    return {"ok": True, **counts}


# ---------- income sources ----------

class IncomeCreate(BaseModel):
    name: str
    amount: float = 0


class IncomePatch(BaseModel):
    name: str | None = None
    amount: float | None = None


def _income_out(row: IncomeSource) -> dict:
    return {"id": row.id, "name": row.name,
            "amount": _dollars(row.amount_cents)}


@router.get("/income")
def get_income() -> list[dict]:
    with Session(engine) as s:
        return [_income_out(r) for r in s.exec(select(IncomeSource)).all()]


@router.post("/income", status_code=201)
def create_income(body: IncomeCreate) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "income source name required")
    with Session(engine) as s:
        row = IncomeSource(name=name, amount_cents=_cents(body.amount))
        s.add(row)
        s.commit()
        s.refresh(row)
        return _income_out(row)


@router.patch("/income/{income_id}")
def patch_income(income_id: int, body: IncomePatch) -> dict:
    with Session(engine) as s:
        row = s.get(IncomeSource, income_id)
        if row is None:
            raise HTTPException(404, "income source not found")
        if body.name is not None and body.name.strip():
            row.name = body.name.strip()
        if body.amount is not None:
            row.amount_cents = _cents(body.amount)
        s.add(row)
        s.commit()
        s.refresh(row)
        return _income_out(row)


@router.delete("/income/{income_id}")
def delete_income(income_id: int) -> dict:
    with Session(engine) as s:
        row = s.get(IncomeSource, income_id)
        if row is None:
            raise HTTPException(404, "income source not found")
        s.delete(row)
        s.commit()
    return {"ok": True, "deleted": income_id}


# ---------- manual accounts ----------

class AccountCreate(BaseModel):
    name: str
    kind: str = "checking"
    balance: float | None = None
    apy: float | None = None
    card_key: str | None = None


class AccountPatch(BaseModel):
    name: str | None = None
    balance: float | None = None
    apy: float | None = None
    card_key: str | None = None


def _account_out(a: Account) -> dict:
    return {"id": a.id, "name": a.name, "kind": a.kind,
            "balance": _dollars(a.balance_cents), "apy": a.apy_bps / 100,
            "card_key": a.card_key, "is_manual": a.is_manual}


@router.post("/accounts", status_code=201)
def create_account(body: AccountCreate) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(422, "account name required")
    with Session(engine) as s:
        acct = Account(
            name=name,
            kind=body.kind,
            is_manual=True,
            balance_cents=_cents(body.balance) if body.balance is not None else 0,
            apy_bps=_bps(body.apy) if body.apy is not None else 0,
            card_key=body.card_key,
        )
        s.add(acct)
        s.flush()
        if body.balance is not None:
            _snapshot_balance(s, acct.id, acct.balance_cents)
        s.commit()
        s.refresh(acct)
        return _account_out(acct)


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, body: AccountPatch) -> dict:
    provided = body.model_fields_set
    with Session(engine) as s:
        acct = s.get(Account, account_id)
        if acct is None:
            raise HTTPException(404, "account not found")
        if "name" in provided and body.name is not None and body.name.strip():
            acct.name = body.name.strip()
        # card_key may be set to null on purpose to clear the mapping.
        if "card_key" in provided:
            acct.card_key = body.card_key
        if "apy" in provided and body.apy is not None:
            acct.apy_bps = _bps(body.apy)
        if "balance" in provided and body.balance is not None:
            acct.balance_cents = _cents(body.balance)
            _snapshot_balance(s, acct.id, acct.balance_cents)
        s.add(acct)
        s.commit()
        s.refresh(acct)
        return _account_out(acct)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int) -> dict:
    with Session(engine) as s:
        acct = s.get(Account, account_id)
        if acct is None:
            raise HTTPException(404, "account not found")
        if not acct.is_manual:
            raise HTTPException(
                409, "only manual accounts can be deleted; unlink the "
                     "connection instead")
        has_txn = s.exec(
            select(Transaction.txn_uid)
            .where(Transaction.account_id == account_id)
            .limit(1)).first()
        if has_txn is not None:
            raise HTTPException(409, "account has transactions and cannot "
                                     "be deleted")
        for snap in s.exec(select(BalanceSnapshot)
                           .where(BalanceSnapshot.account_id == account_id)).all():
            s.delete(snap)
        # Flush the snapshot deletes first; without ORM relationships the
        # unit of work does not order these deletes before the parent row.
        s.flush()
        s.delete(acct)
        s.commit()
    return {"ok": True, "deleted": account_id}


# ---------- savings plan ----------

class SavingsBody(BaseModel):
    monthly: float | None = None
    goal: float | None = None
    note: str | None = None


def _load_savings(session: Session) -> dict:
    row = session.get(Setting, SAVINGS_KEY)
    if row is None:
        return {}
    try:
        data = json.loads(row.value_json or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_setting(session: Session, key: str, data: dict) -> None:
    row = session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value_json=json.dumps(data))
    else:
        row.value_json = json.dumps(data)
    session.add(row)


def _savings_out(data: dict) -> dict:
    return {"monthly": _dollars(int(data.get("monthly_cents") or 0)),
            "goal": _dollars(int(data.get("goal_cents") or 0)),
            "note": str(data.get("note") or "")}


@router.get("/savings-plan")
def get_savings_plan() -> dict:
    with Session(engine) as s:
        return _savings_out(_load_savings(s))


@router.put("/savings-plan")
def put_savings_plan(body: SavingsBody) -> dict:
    with Session(engine) as s:
        data = _load_savings(s)
        if body.monthly is not None:
            data["monthly_cents"] = _cents(body.monthly)
        if body.goal is not None:
            data["goal_cents"] = _cents(body.goal)
        if body.note is not None:
            data["note"] = body.note
        _save_setting(s, SAVINGS_KEY, data)
        s.commit()
        return _savings_out(data)


# ---------- net worth ----------

def compute_networth(session: Session, today: date | None = None) -> dict:
    """Forward filled net worth series from balance snapshots.

    For each distinct snapshot date ascending, the total is the sum over all
    accounts of each account's most recent snapshot on or before that date.
    An account with zero snapshots but a nonzero stored balance contributes
    one synthetic snapshot dated today, in memory only, never persisted."""
    today = today or date.today()
    accounts = session.exec(select(Account)).all()
    snaps = session.exec(select(BalanceSnapshot)
                         .order_by(BalanceSnapshot.taken_on)).all()

    by_account: dict[int, list[tuple[date, int]]] = defaultdict(list)
    for snap in snaps:
        by_account[snap.account_id].append((snap.taken_on, snap.balance_cents))
    for acct in accounts:
        if acct.id not in by_account and acct.balance_cents != 0:
            by_account[acct.id].append((today, acct.balance_cents))

    # Group the per-account updates by date, then walk dates ascending while
    # carrying each account's last seen value forward.
    updates: dict[date, list[tuple[int, int]]] = defaultdict(list)
    for acct_id, rows in by_account.items():
        for d, cents in rows:
            updates[d].append((acct_id, cents))
    current: dict[int, int] = {}
    series: list[dict] = []
    for d in sorted(updates):
        for acct_id, cents in updates[d]:
            current[acct_id] = cents
        series.append({"date": d.isoformat(),
                       "total": _dollars(sum(current.values()))})

    accounts_out = []
    for acct in accounts:
        rows = by_account.get(acct.id)
        latest = rows[-1][1] if rows else acct.balance_cents
        accounts_out.append({"id": acct.id, "name": acct.name,
                             "kind": acct.kind, "latest": _dollars(latest)})
    return {"series": series, "accounts": accounts_out}


@router.get("/networth")
def get_networth() -> dict:
    with Session(engine) as s:
        return compute_networth(s)


# ---------- localStorage migration ----------

def apply_local_blob(session: Session, blob: dict) -> dict:
    """Map the frontend's 'tally:v2' localStorage blob into server rows.

    Blob shape, from DEFAULTS in frontend/index.html:
      income:   [{name, amount}]              monthly dollars
      accounts: [{name, balance, apy, kind}]  dollars, apy is a percent
      savings:  {monthly, goal, autoInto}     dollars plus an account name
      targets:  {category: dollars}
    The moves and incomeNote keys are UI state and are not migrated.
    Idempotent: every section upserts by natural key."""
    counts = {"budgets": 0, "income": 0, "accounts_updated": 0,
              "accounts_created": 0, "savings": 0}

    targets = blob.get("targets") or {}
    if isinstance(targets, dict):
        res = apply_budget_targets(session, targets, replace=False)
        counts["budgets"] = res["upserted"]

    income = blob.get("income") or []
    if isinstance(income, list):
        existing = {r.name.strip().lower(): r
                    for r in session.exec(select(IncomeSource)).all()}
        for entry in income:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            cents = _cents(entry.get("amount") or 0)
            row = existing.get(name.lower())
            if row is None:
                row = IncomeSource(name=name, amount_cents=cents)
                existing[name.lower()] = row
            else:
                row.amount_cents = cents
            session.add(row)
            counts["income"] += 1

    accounts = blob.get("accounts") or []
    if isinstance(accounts, list):
        by_name = {a.name.strip().lower(): a
                   for a in session.exec(select(Account)).all()}
        for entry in accounts:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            balance = entry.get("balance")
            apy = entry.get("apy")
            acct = by_name.get(name.lower())
            if acct is None:
                acct = Account(
                    name=name,
                    kind=str(entry.get("kind") or "checking"),
                    is_manual=True,
                    balance_cents=_cents(balance) if balance is not None else 0,
                    apy_bps=_bps(apy) if apy is not None else 0,
                )
                session.add(acct)
                session.flush()
                by_name[name.lower()] = acct
                counts["accounts_created"] += 1
                if balance is not None:
                    _snapshot_balance(session, acct.id, acct.balance_cents)
            else:
                if balance is not None:
                    acct.balance_cents = _cents(balance)
                    _snapshot_balance(session, acct.id, acct.balance_cents)
                if apy is not None:
                    acct.apy_bps = _bps(apy)
                session.add(acct)
                counts["accounts_updated"] += 1

    savings = blob.get("savings") or {}
    if isinstance(savings, dict) and savings:
        data = _load_savings(session)
        if savings.get("monthly") is not None:
            data["monthly_cents"] = _cents(savings["monthly"])
        if savings.get("goal") is not None:
            data["goal_cents"] = _cents(savings["goal"])
        if savings.get("autoInto") is not None:
            data["auto_into"] = str(savings["autoInto"])
        _save_setting(session, SAVINGS_KEY, data)
        counts["savings"] = 1

    return counts


@router.post("/migrate-local")
def migrate_local(blob: dict) -> dict:
    if not isinstance(blob, dict):
        raise HTTPException(422, "body must be the tally:v2 config object")
    with Session(engine) as s:
        counts = apply_local_blob(s, blob)
        s.commit()
    return {"ok": True, **counts}
