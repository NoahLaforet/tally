"""Month-to-date pace: the number the app opens to.

Everything is pinned to a fixed `today` so the trailing-month shape is
deterministic. The scenario is built so the typical fraction is exactly 0.5,
which keeps the projection arithmetic exact.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.canonical import make_txn_uid
from app.pace import compute_pace, load_cap_cents, save_cap_cents
from app.spend import instance_lexicons, spend_amount
from app.models import Transaction

TODAY = date(2026, 7, 15)  # day 15 of a 31-day month


@pytest.fixture
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _txn(s, posted, cents, desc, **kw):
    t = Transaction(
        txn_uid=make_txn_uid("t", posted, cents, desc, kw.get("source_line", 0)),
        account_id=1, posted_date=posted, amount_cents=cents,
        raw_description=desc, norm_merchant=desc.upper(),
        category=kw.pop("category", "grocery"), **kw)
    s.add(t)
    return t


def _base_scenario(s):
    """June and May each: $300 through day 15, $300 after -> typical 0.5 shape.
    July (current): $200 on the 3rd, $100 on the 10th -> $300 so far."""
    _txn(s, date(2026, 6, 8), -300_00, "TJ JUNE EARLY", source_line=1)
    _txn(s, date(2026, 6, 22), -300_00, "TJ JUNE LATE", source_line=2)
    _txn(s, date(2026, 5, 12), -300_00, "TJ MAY EARLY", source_line=3)
    _txn(s, date(2026, 5, 28), -300_00, "TJ MAY LATE", source_line=4)
    _txn(s, date(2026, 7, 3), -200_00, "TJ JULY ONE", source_line=5)
    _txn(s, date(2026, 7, 10), -100_00, "TJ JULY TWO", source_line=6)
    s.commit()


def test_empty_db_does_not_crash(db):
    p = compute_pace(db, today=TODAY)
    assert p["spent"] == 0
    assert p["typical_monthly"] == 0
    assert p["projected"] == 0
    assert p["cap"] is None and p["cap_set"] is False
    assert p["left_to_spend"] is None
    assert p["state"] == "under"
    assert p["trailing_months"] == 0
    assert p["day_of_month"] == 15 and p["days_in_month"] == 31


def test_core_numbers(db):
    _base_scenario(db)
    p = compute_pace(db, today=TODAY)
    assert p["spent"] == 300.0
    assert p["typical_by_today"] == 300.0
    assert p["typical_monthly"] == 600.0
    assert p["projected"] == 600.0          # 300 / 0.5
    assert p["pace_delta"] == 0.0           # spent == typical_by_today
    assert p["trailing_months"] == 2
    assert p["suggested_cap"] == 600.0
    assert p["month"] == "July 2026"


def test_cap_projection_flags_over_before_you_get_there(db):
    _base_scenario(db)
    save_cap_cents(db, 550_00)
    db.commit()
    p = compute_pace(db, today=TODAY)
    assert p["cap"] == 550.0 and p["cap_set"] is True
    assert p["left_to_spend"] == 250.0      # 550 - 300, still money left today
    assert p["projected"] == 600.0          # but projected to blow past 550
    assert p["state"] == "over"


def test_cap_roundtrip_and_clear(db):
    assert load_cap_cents(db) is None
    save_cap_cents(db, 1234_00)
    db.commit()
    assert load_cap_cents(db) == 1234_00
    save_cap_cents(db, None)
    db.commit()
    assert load_cap_cents(db) is None
    save_cap_cents(db, 500_00)
    db.commit()
    save_cap_cents(db, 0)  # <=0 clears it too
    db.commit()
    assert load_cap_cents(db) is None


def test_non_spend_rows_never_count(db):
    _base_scenario(db)
    # None of these are the owner's spending; spent-so-far must stay $300.
    _txn(db, date(2026, 7, 5), -500_00, "MOVE TO SAVINGS", is_transfer=True)
    _txn(db, date(2026, 7, 6), -400_00, "COVERED DINNER", reimbursement="group")
    _txn(db, date(2026, 7, 7), -250_00, "DRAFTKINGS BET")
    _txn(db, date(2026, 7, 8), -900_00, "CREDIT CARD AUTO PAY")
    _txn(db, date(2026, 7, 9), 700_00, "PAYCHECK DEPOSIT")  # inflow
    db.commit()
    p = compute_pace(db, today=TODAY)
    assert p["spent"] == 300.0


def test_pace_agrees_with_shared_spend_predicate(db):
    """The month-to-date figure must equal an independent sum over the same
    predicate, so pace can never drift from the Spending view."""
    _base_scenario(db)
    _txn(db, date(2026, 7, 7), -250_00, "DRAFTKINGS BET")  # excluded
    _txn(db, date(2026, 7, 8), -40_00, "COFFEE", category="dining")  # counts
    db.commit()
    lex = instance_lexicons(db)
    from sqlmodel import select
    month_start = date(2026, 7, 1)
    manual = 0
    for t in db.exec(select(Transaction)).all():
        if t.posted_date < month_start or t.posted_date > TODAY:
            continue
        amt = spend_amount(t, lex["gambling"], lex["nonconsumption"])
        if amt:
            manual += amt
    p = compute_pace(db, today=TODAY)
    assert round(p["spent"] * 100) == manual
    assert manual == 340_00  # 200 + 100 + 40, draftkings excluded
