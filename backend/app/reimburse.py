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
# Exact combos stop at pairs: three unrelated inflows coincidentally summing
# to a charge is common enough to be noise, and a real 3-way group buy still
# surfaces as a candidate via its P2P legs.
MAX_LEGS = 2

# Inflows that are never repayments.
_NOT_REPAYMENT = ("payroll", "direct dep", "interest", "dividend", "refund",
                  "cash back", "daily cash", "tax ref")

# The fuzzy candidate tier only trusts person-to-person inflows; anything
# else glued onto a big charge reads as noise.
_P2P = ("zelle", "venmo", "cash app", "cashapp", "paypal", "apple cash")


# A large charge with partial paybacks is worth a human look even without an
# exact-sum match (venmo legs round differently, someone still owes a share).
# Under 40 percent covered the pairing is usually coincidence, which erodes
# trust in the whole queue, so those stay quiet.
CANDIDATE_MIN_COVERAGE = 0.4
CANDIDATE_CAP = 12


def find_suggestions(session: Session, min_cents: int = MIN_OUTFLOW_CENTS,
                     window_days: int = WINDOW_DAYS) -> dict:
    """Two tiers: 'exact' (inflows sum back to the cent, high confidence) and
    'candidates' (large charges at least partially covered by inflows in the
    window; the user judges). Both carry the matched inflow legs."""
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

    def leg(l: Transaction) -> dict:
        return {"uid": l.txn_uid, "date": l.posted_date.isoformat(),
                "merchant": l.norm_merchant,
                "amount": round(l.amount_cents / 100, 2),
                "account": accounts.get(l.account_id, "")}

    def charge(o: Transaction, legs: list[Transaction], coverage: float) -> dict:
        return {"uid": o.txn_uid,
                "date": o.posted_date.isoformat(),
                "merchant": o.norm_merchant,
                "description": o.raw_description,
                "amount": round(-o.amount_cents / 100, 2),
                "account": accounts.get(o.account_id, ""),
                "coverage": round(coverage, 2),
                "repaid_by": [leg(l) for l in legs]}

    used: set[str] = set()
    exact: list[dict] = []
    candidates: list[dict] = []
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
            for combo in combinations(pool, MAX_LEGS):
                if sum(c.amount_cents for c in combo) == target:
                    legs = list(combo)
                    break
        if legs:
            used.update(l.txn_uid for l in legs)
            exact.append(charge(o, legs, 1.0))
            continue
        # Partial coverage: at most two P2P inflows (zelle/venmo style), each
        # offered under one charge only, so the queue never shows the same
        # payment glued onto several charges.
        p2p = [i for i in pool
               if any(k in (i.norm_merchant + " " + i.raw_description).lower()
                      for k in _P2P)]
        p2p.sort(key=lambda i: -i.amount_cents)
        part: list[Transaction] = []
        covered = 0
        for i in p2p:  # greedy, never past the charge amount
            if len(part) == 2:
                break
            if covered + i.amount_cents <= target:
                part.append(i)
                covered += i.amount_cents
        if part and covered >= target * CANDIDATE_MIN_COVERAGE:
            used.update(l.txn_uid for l in part)
            candidates.append(charge(o, part, covered / target))

    candidates.sort(key=lambda c: -c["amount"])
    return {"exact": exact, "candidates": candidates[:CANDIDATE_CAP]}


@router.get("/suggestions")
def api_suggestions(min_dollars: float = 150.0, window_days: int = WINDOW_DAYS) -> dict:
    min_cents = max(1, round(min_dollars * 100))
    window_days = max(1, min(60, window_days))
    with Session(engine) as s:
        return find_suggestions(s, min_cents=min_cents, window_days=window_days)


@router.get("/rules")
def api_rules() -> list[dict]:
    from .models import ReimbursementRule

    with Session(engine) as s:
        return [{"merchant": r.norm_merchant, "kind": r.kind,
                 "created_at": r.created_at.isoformat() if r.created_at else None}
                for r in s.exec(select(ReimbursementRule)).all()]


@router.delete("/rules/{merchant}")
def api_delete_rule(merchant: str, unmark: bool = True) -> dict:
    """Drop a standing order; by default also unmark that merchant's rows."""
    from fastapi import HTTPException

    from .models import ReimbursementRule

    with Session(engine) as s:
        rule = s.get(ReimbursementRule, merchant)
        if rule is None:
            raise HTTPException(404, "no rule for that merchant")
        s.delete(rule)
        cleared = 0
        if unmark:
            rows = s.exec(select(Transaction)
                          .where(Transaction.norm_merchant == merchant)
                          .where(Transaction.reimbursement != None)  # noqa: E711
                          ).all()
            for t in rows:
                t.reimbursement = None
                s.add(t)
            cleared = len(rows)
        s.commit()
    return {"ok": True, "cleared": cleared}
