"""This month vs last, per category, over the SAME day range.

Comparing a partial July against a full June would flatter or scare you for no
reason, so this lines up equal windows: the 1st through today this month against
the 1st through the same day last month. Same spend predicate as everywhere else
(app.spend). Money is integer cents until the JSON boundary.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from .auth import require_user
from .db import engine
from .models import Transaction
from .spend import instance_lexicons, spend_amount

router = APIRouter(prefix="/api", tags=["compare"],
                   dependencies=[Depends(require_user)])


def _dollars(cents: int) -> float:
    return round(cents / 100, 2)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def compute_compare(session: Session, today: date | None = None) -> dict:
    today = today or date.today()
    day = today.day
    cur_start = date(today.year, today.month, 1)
    py, pm = _prev_month(today.year, today.month)
    prev_days = calendar.monthrange(py, pm)[1]
    prev_start = date(py, pm, 1)
    prev_end = date(py, pm, min(day, prev_days))

    lex = instance_lexicons(session)
    gambling, noncons = lex["gambling"], lex["nonconsumption"]
    txns = session.exec(
        select(Transaction)
        .where(Transaction.posted_date >= prev_start)
        .where(Transaction.posted_date <= today)).all()

    cur: dict[str, int] = defaultdict(int)
    prev: dict[str, int] = defaultdict(int)
    for t in txns:
        amt = spend_amount(t, gambling, noncons)
        if amt is None:
            continue
        cat = t.category or "other"
        d = t.posted_date
        if cur_start <= d <= today:
            cur[cat] += amt
        elif prev_start <= d <= prev_end:  # equal-length window last month
            prev[cat] += amt

    cats = sorted(set(cur) | set(prev), key=lambda c: -cur.get(c, 0))
    rows = [{"id": c, "current": _dollars(cur.get(c, 0)),
             "previous": _dollars(prev.get(c, 0)),
             "delta": _dollars(cur.get(c, 0) - prev.get(c, 0))} for c in cats]
    return {
        "day": day,
        "current_label": f"{calendar.month_abbr[today.month]} 1–{day}",
        "previous_label": f"{calendar.month_abbr[pm]} 1–{min(day, prev_days)}",
        "current_total": _dollars(sum(cur.values())),
        "previous_total": _dollars(sum(prev.values())),
        "delta_total": _dollars(sum(cur.values()) - sum(prev.values())),
        "categories": rows,
    }


@router.get("/compare")
def api_compare() -> dict:
    with Session(engine) as session:
        return compute_compare(session)
