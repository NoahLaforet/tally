"""Am I on pace this month? The daily-glance answer.

Tally's whole reason to open is this one question. The Overview leads with it and
the Budget tab hangs its overall cap off it. Everything here uses the same
definition of spending as the dashboard (app.spend), so the pace figure can
never disagree with the Spending view.

The "typical by today" reference and the month-end projection both come from the
shape of the last few months, not a flat line. If rent lands on the 1st, the
trailing shape knows you are usually well ahead by day 2 and does not cry wolf;
a flat "spend / days_in_month" line would. Money is integer cents until the
JSON boundary.
"""

from __future__ import annotations

import calendar
import json
from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import require_user
from .db import engine
from .models import Setting, Transaction
from .spend import instance_lexicons, spend_amount

# Router-level auth on top of the structural gate, defense in depth.
router = APIRouter(prefix="/api", tags=["pace"],
                   dependencies=[Depends(require_user)])

CAP_KEY = "overall_budget"        # Setting row that stores {"cap_cents": N}
TRAILING_MONTHS = 3               # full prior months that define "typical"
SUGGEST_ROUND_CENTS = 50_00       # round the auto-suggested cap to the $50


def _dollars(cents: int) -> float:
    return round(cents / 100, 2)


# ---------- overall cap persistence (Setting key/value, like the savings plan) ----------

def load_cap_cents(session: Session) -> int | None:
    row = session.get(Setting, CAP_KEY)
    if row is None:
        return None
    try:
        data = json.loads(row.value_json or "{}")
    except json.JSONDecodeError:
        return None
    cap = data.get("cap_cents")
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def save_cap_cents(session: Session, cap_cents: int | None) -> None:
    """Set the overall monthly cap, or clear it when cap_cents is None/<=0."""
    row = session.get(Setting, CAP_KEY)
    if cap_cents is None or cap_cents <= 0:
        if row is not None:
            session.delete(row)
        return
    payload = json.dumps({"cap_cents": int(cap_cents)})
    if row is None:
        session.add(Setting(key=CAP_KEY, value_json=payload))
    else:
        row.value_json = payload
        session.add(row)


# ---------- month math ----------

def _prior_full_months(today: date, n: int) -> list[tuple[int, int]]:
    """The n full calendar months before today's month, newest first, as
    (year, month) pairs."""
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append((y, m))
    return out


def compute_pace(session: Session, today: date | None = None) -> dict:
    today = today or date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    day_of_month = today.day
    month_start = date(today.year, today.month, 1)

    lex = instance_lexicons(session)
    gambling, noncons = lex["gambling"], lex["nonconsumption"]

    prior = _prior_full_months(today, TRAILING_MONTHS)
    prior_set = set(prior)
    earliest = date(prior[-1][0], prior[-1][1], 1) if prior else month_start

    # One scan of everything from the earliest trailing month through today.
    txns = session.exec(
        select(Transaction)
        .where(Transaction.posted_date >= earliest)
        .where(Transaction.posted_date <= today)).all()

    spent_mtd = 0
    prior_total: dict[tuple[int, int], int] = {ym: 0 for ym in prior}
    prior_through: dict[tuple[int, int], int] = {ym: 0 for ym in prior}
    for t in txns:
        amt = spend_amount(t, gambling, noncons)
        if amt is None:
            continue
        d = t.posted_date
        if d >= month_start:
            spent_mtd += amt
            continue
        ym = (d.year, d.month)
        if ym in prior_set:
            prior_total[ym] += amt
            # Spend during days 1..today's day-of-month in that prior month.
            # A prior month shorter than today just contributes its whole self.
            if d.day <= day_of_month:
                prior_through[ym] += amt

    with_data = [ym for ym in prior if prior_total[ym] > 0]
    n = len(with_data)
    typical_monthly = round(sum(prior_total[ym] for ym in with_data) / n) if n else 0
    typical_by_today = round(sum(prior_through[ym] for ym in with_data) / n) if n else 0

    # Project the finish. Prefer the trailing shape: if you are usually X% of
    # the way through your spend by today, scale what you have spent by 1/X.
    # This stays sane on day 2 when a flat spent*days_in_month would explode.
    # Fall back to the flat line only when there is no usable shape.
    if typical_monthly > 0 and typical_by_today > 0:
        frac = typical_by_today / typical_monthly
        projected = round(spent_mtd / frac)
    elif day_of_month > 0:
        projected = round(spent_mtd / day_of_month * days_in_month)
    else:
        projected = spent_mtd

    cap_cents = load_cap_cents(session)
    cap_set = cap_cents is not None
    left = (cap_cents - spent_mtd) if cap_set else None

    # Suggested cap: the typical month rounded up to a tidy $50.
    if typical_monthly > 0:
        suggested = -(-typical_monthly // SUGGEST_ROUND_CENTS) * SUGGEST_ROUND_CENTS
    else:
        suggested = 0

    # State for color, copy, and (later) the alert engine. "over" is a real
    # problem (already past cap, or projected past it once there are a few days
    # of signal); "watch" is running hotter than your own normal; else "under".
    proj_trustworthy = day_of_month >= 4
    if cap_set and (spent_mtd > cap_cents or (proj_trustworthy and projected > cap_cents)):
        state = "over"
    elif typical_by_today > 0 and spent_mtd > typical_by_today:
        state = "watch"
    else:
        state = "under"

    return {
        "month": f"{calendar.month_name[today.month]} {today.year}",
        "day_of_month": day_of_month,
        "days_in_month": days_in_month,
        "spent": _dollars(spent_mtd),
        "typical_by_today": _dollars(typical_by_today),
        "typical_monthly": _dollars(typical_monthly),
        "projected": _dollars(projected),
        "cap": _dollars(cap_cents) if cap_set else None,
        "cap_set": cap_set,
        "suggested_cap": _dollars(suggested),
        "left_to_spend": _dollars(left) if left is not None else None,
        "pace_delta": _dollars(spent_mtd - typical_by_today),
        "state": state,
        "trailing_months": n,
    }


@router.get("/pace")
def api_pace() -> dict:
    with Session(engine) as session:
        return compute_pace(session)


class CapBody(BaseModel):
    cap: float | None = None  # dollars; null or <=0 clears the cap


@router.put("/pace/cap")
def api_set_cap(body: CapBody) -> dict:
    with Session(engine) as session:
        cents = None if body.cap is None else round(float(body.cap) * 100)
        save_cap_cents(session, cents)
        session.commit()
        return compute_pace(session)
