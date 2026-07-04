"""Subscription detection engine.

Scans posted transactions for recurring charges. The cadence comes from the
median gap between consecutive charge dates, the amount check throws out
variable spenders like grocery stores, and the results merge into the
Subscription table without duplicating the seeded rows. No ML anywhere,
every decision is a rule you can explain in one sentence.

Money rule: all arithmetic is integer cents. Dollars appear only at the JSON
boundary. Median comparisons use twice-the-median so even-length groups stay
in exact integer math.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import require_user
from .db import get_session
from .models import Account, Card, Subscription, Transaction

# Router-level auth on top of the global gate, defense in depth.
router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"],
                   dependencies=[Depends(require_user)])

# Median-gap bands in days mapped to a canonical cadence_days. A median gap
# outside every band means the merchant is not recurring.
_CADENCE_BANDS = [
    (5, 9, 7),        # weekly
    (25, 35, 30),     # monthly
    (55, 70, 60),     # bimonthly
    (80, 100, 91),    # quarterly
    (330, 400, 365),  # yearly
]

# Categories where people also spend variably. A grocery run is not a
# subscription but a flat DashPass fee is, so these require near identical
# amounts instead of the loose band.
_TIGHT_CATEGORIES = {"grocery", "gas", "dining"}

_LOOSE_PCT = 25
_TIGHT_PCT = 2

_MIN_CHARGES = 3

_ALLOWED_STATUSES = {"keep", "move", "review", "verify", "canceled",
                     "discretionary"}


def _median2(values: list[int]) -> int:
    """Twice the median as an exact integer. Avoids the .5 float that a plain
    median produces on even-length lists."""
    s = sorted(values)
    n = len(s)
    if n % 2:
        return 2 * s[n // 2]
    return s[n // 2 - 1] + s[n // 2]


def _div_round(numerator: int, denominator: int) -> int:
    """Integer division rounded half up, no floats."""
    return (2 * numerator + denominator) // (2 * denominator)


def _cadence_days(dates: list[date]) -> int | None:
    """Map the median gap between consecutive charges to a cadence, or None."""
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    med2 = _median2(gaps)
    for lo, hi, cadence in _CADENCE_BANDS:
        if 2 * lo <= med2 <= 2 * hi:
            return cadence
    return None


def _stable(amounts: list[int], latest: int, tight: bool) -> bool:
    """True when the latest amount sits close enough to the median amount."""
    med2 = _median2(amounts)
    pct = _TIGHT_PCT if tight else _LOOSE_PCT
    # abs(latest - median) <= median * pct / 100, scaled by 2 to stay integer.
    return abs(2 * latest - med2) * 100 <= med2 * pct


def _monthly_cents(latest: int, cadence: int) -> int:
    """Normalize the latest charge to a monthly figure in integer cents."""
    if cadence == 7:
        # weekly times 4.33, done as x433/100 so no float touches money
        return _div_round(latest * 433, 100)
    if cadence == 30:
        return latest
    if cadence == 60:
        return _div_round(latest, 2)
    if cadence == 91:
        return _div_round(latest, 3)
    return _div_round(latest, 12)


def _price_creep(amounts_chrono: list[int], latest: int) -> bool:
    """Latest charge beats the previous distinct amount by more than 2 percent
    and by at least 50 cents."""
    prev = None
    for a in reversed(amounts_chrono[:-1]):
        if a != latest:
            prev = a
            break
    if prev is None:
        return False
    diff = latest - prev
    return diff >= 50 and diff * 100 > prev * 2


def _forgotten(last_seen: date, cadence: int, today: date) -> bool:
    """No charge for more than two full cycles reads as forgotten."""
    return (today - last_seen).days > 2 * cadence


def _modal_category(group: list[Transaction]) -> str:
    counts = Counter(t.category or "other" for t in group)
    return counts.most_common(1)[0][0]


def _candidate_groups(session: Session) -> dict[str, list[Transaction]]:
    """Non-transfer outflows grouped by merchant, chronological within each
    group, only groups with enough charges to infer a cadence."""
    txns = session.exec(
        select(Transaction)
        .where(Transaction.is_transfer == False)  # noqa: E712 (SQL expression)
        .where(Transaction.amount_cents < 0)
        .order_by(Transaction.posted_date, Transaction.txn_uid)
    ).all()
    groups: dict[str, list[Transaction]] = {}
    for t in txns:
        if not t.norm_merchant:
            continue
        # Fronted bills (AT&T family plan style) are repaid, so they are not
        # part of the owner's recurring spend; transfers never are.
        if t.reimbursement in ("group", "thirdparty") or t.category == "transfer":
            continue
        groups.setdefault(t.norm_merchant, []).append(t)
    return {m: g for m, g in groups.items() if len(g) >= _MIN_CHARGES}


def _match_existing(subs: list[Subscription],
                    merchant: str) -> Subscription | None:
    """Match by norm_merchant first, then case-insensitive name containment in
    either direction, so 'spotify' merges into a seeded 'Spotify Premium'.

    Containment only counts when the shorter side is a real word (>= 5 chars)
    and the match sits on a word boundary; otherwise short merchants swallow
    unrelated rows ('EA' would absorb 'Peacock', 'Uber' would absorb
    'Uber Eats')."""
    for s in subs:
        if s.norm_merchant and s.norm_merchant == merchant:
            return s
    m = merchant.lower()
    for s in subs:
        n = (s.name or "").strip().lower()
        if not n or len(min(m, n, key=len)) < 5:
            continue
        shorter, longer = (m, n) if len(m) <= len(n) else (n, m)
        if re.search(rf"(^|\W){re.escape(shorter)}($|\W)", longer):
            return s
    return None


def _card_key_for(session: Session, txn: Transaction) -> str | None:
    if txn.account_id is None:
        return None
    acct = session.get(Account, txn.account_id)
    return acct.card_key if acct else None


def _best_card(cards: list[Card], category: str) -> str | None:
    """The card whose rules matrix pays the most bps for this category. Ties
    keep the first card by id so the answer is stable."""
    best_key: str | None = None
    best_bps = -1
    for c in cards:
        try:
            rules = json.loads(c.rules_json or "{}")
        except ValueError:
            continue
        bps = rules.get(category, rules.get("other", 0))
        if not isinstance(bps, int):
            continue
        if bps > best_bps:
            best_bps = bps
            best_key = c.key
    return best_key


def detect_subscriptions(session: Session, today: date | None = None) -> dict:
    """Run the detector over all transactions and merge into Subscription.

    Existing rows keep everything the user may have touched. Rows with status
    other than 'review' count as touched: their measurement fields still
    update but status, moved, manage_url and the card routing never change.
    Returns {detected, updated, flagged_creep, flagged_forgotten}.
    """
    today = today or date.today()
    cards = session.exec(select(Card).order_by(Card.id)).all()
    subs = list(session.exec(select(Subscription)).all())
    n_new = n_updated = n_creep = n_forgotten = 0

    for merchant, group in sorted(_candidate_groups(session).items()):
        dates = [t.posted_date for t in group]
        cadence = _cadence_days(dates)
        if cadence is None:
            continue

        amounts = [abs(t.amount_cents) for t in group]
        latest_txn = group[-1]
        latest = abs(latest_txn.amount_cents)
        category = _modal_category(group)
        if not _stable(amounts, latest,
                       tight=category in _TIGHT_CATEGORIES):
            continue

        flag: str | None = None
        if _forgotten(dates[-1], cadence, today):
            flag = "forgotten"
            n_forgotten += 1
        elif _price_creep(amounts, latest):
            flag = "price_creep"
            n_creep += 1

        monthly = _monthly_cents(latest, cadence)
        existing = _match_existing(subs, merchant)
        if existing is not None:
            existing.monthly_cents = monthly
            existing.cadence_days = cadence
            existing.last_amount_cents = latest
            existing.last_seen_on = dates[-1]
            existing.flag = flag
            existing.norm_merchant = merchant
            if existing.status == "review":
                # Untouched row, safe to refresh the card routing too.
                existing.current_card = _card_key_for(session, latest_txn)
                existing.recommended_card = _best_card(cards, category)
            session.add(existing)
            n_updated += 1
        else:
            row = Subscription(
                name=merchant,
                monthly_cents=monthly,
                category=category,
                current_card=_card_key_for(session, latest_txn),
                recommended_card=_best_card(cards, category),
                status="review",
                detected=True,
                cadence_days=cadence,
                last_amount_cents=latest,
                last_seen_on=dates[-1],
                flag=flag,
                norm_merchant=merchant,
            )
            session.add(row)
            # Keep the in-memory list current so a rerun in the same call
            # cannot create a duplicate.
            subs.append(row)
            n_new += 1

    session.commit()
    return {"detected": n_new, "updated": n_updated,
            "flagged_creep": n_creep, "flagged_forgotten": n_forgotten}


def _sub_dict(s: Subscription) -> dict:
    """JSON shape for one subscription, dollars at the boundary."""
    return {
        "id": s.id,
        "name": s.name,
        "monthly": round(s.monthly_cents / 100, 2),
        "category": s.category,
        "current_card": s.current_card,
        "recommended_card": s.recommended_card,
        "status": s.status,
        "manage_url": s.manage_url,
        "moved": s.moved,
        "detected": s.detected,
        "cadence_days": s.cadence_days,
        "last_amount": (round(s.last_amount_cents / 100, 2)
                        if s.last_amount_cents is not None else None),
        "last_seen_on": s.last_seen_on.isoformat() if s.last_seen_on else None,
        "flag": s.flag,
        "norm_merchant": s.norm_merchant,
    }


class SubscriptionPatch(BaseModel):
    """PATCH body. monthly is dollars; it becomes cents exactly once here."""

    status: str | None = None
    moved: bool | None = None
    current_card: str | None = None
    recommended_card: str | None = None
    manage_url: str | None = None
    monthly: float | None = None


@router.post("/detect")
def run_detect(session: Session = Depends(get_session)) -> dict:
    """Run the detector and return the summary counts."""
    return detect_subscriptions(session)


@router.patch("/{sub_id}")
def patch_subscription(sub_id: int, patch: SubscriptionPatch,
                       session: Session = Depends(get_session)) -> dict:
    row = session.get(Subscription, sub_id)
    if row is None:
        raise HTTPException(404, "subscription not found")
    data = patch.model_dump(exclude_unset=True)
    if "status" in data:
        if data["status"] not in _ALLOWED_STATUSES:
            raise HTTPException(
                400, "invalid status; use one of: "
                     + ", ".join(sorted(_ALLOWED_STATUSES)))
        row.status = data["status"]
    if data.get("moved") is not None:
        row.moved = bool(data["moved"])
    for field in ("current_card", "recommended_card", "manage_url"):
        if field in data:
            setattr(row, field, data[field])
    if data.get("monthly") is not None:
        # Dollars in, integer cents stored. This is the only conversion.
        row.monthly_cents = round(float(data["monthly"]) * 100)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _sub_dict(row)


@router.delete("/{sub_id}")
def delete_subscription(sub_id: int,
                        session: Session = Depends(get_session)) -> dict:
    """Remove a row entirely, for false detections."""
    row = session.get(Subscription, sub_id)
    if row is None:
        raise HTTPException(404, "subscription not found")
    session.delete(row)
    session.commit()
    return {"ok": True, "deleted": sub_id}
