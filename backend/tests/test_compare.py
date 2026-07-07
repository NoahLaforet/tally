"""Month-vs-month compare lines up equal day ranges."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.canonical import make_txn_uid
from app.compare import compute_compare
from app.models import Transaction

TODAY = date(2026, 7, 15)


@pytest.fixture
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _txn(s, posted, cents, desc, cat, **kw):
    t = Transaction(
        txn_uid=make_txn_uid("t", posted, cents, desc, kw.get("source_line", 0)),
        account_id=1, posted_date=posted, amount_cents=cents,
        raw_description=desc, norm_merchant=desc.upper(), category=cat, **kw)
    s.add(t)
    return t


def test_same_day_window(db):
    # This month, through the 15th.
    _txn(db, date(2026, 7, 5), -100_00, "DINE A", "dining", source_line=1)
    _txn(db, date(2026, 7, 12), -50_00, "GRO A", "grocery", source_line=2)
    # Last month: one inside the 1-15 window, one AFTER it (must be excluded).
    _txn(db, date(2026, 6, 3), -80_00, "DINE B", "dining", source_line=3)
    _txn(db, date(2026, 6, 20), -200_00, "GRO B", "grocery", source_line=4)
    db.commit()

    r = compute_compare(db, today=TODAY)
    by = {c["id"]: c for c in r["categories"]}
    assert by["dining"]["current"] == 100.0
    assert by["dining"]["previous"] == 80.0
    assert by["dining"]["delta"] == 20.0
    assert by["grocery"]["current"] == 50.0
    assert by["grocery"]["previous"] == 0.0        # June 20 is outside 1-15
    assert r["current_total"] == 150.0
    assert r["previous_total"] == 80.0
    assert r["delta_total"] == 70.0
    assert r["current_label"] == "Jul 1–15"
    assert r["previous_label"] == "Jun 1–15"
    # Categories are ordered by current spend, biggest first.
    assert [c["id"] for c in r["categories"]] == ["dining", "grocery"]


def test_excludes_non_spend(db):
    _txn(db, date(2026, 7, 5), -100_00, "DINE", "dining", source_line=1)
    _txn(db, date(2026, 7, 6), -500_00, "MOVE", "other", is_transfer=True, source_line=2)
    _txn(db, date(2026, 7, 7), 900_00, "PAYCHECK", "other", source_line=3)  # inflow
    db.commit()
    r = compute_compare(db, today=TODAY)
    assert r["current_total"] == 100.0


def test_empty_db(db):
    r = compute_compare(db, today=TODAY)
    assert r["current_total"] == 0 and r["previous_total"] == 0
    assert r["categories"] == []


def test_january_rolls_to_december(db):
    jan = date(2026, 1, 10)
    _txn(db, date(2026, 1, 5), -30_00, "JAN", "dining", source_line=1)
    _txn(db, date(2025, 12, 4), -20_00, "DEC", "dining", source_line=2)
    db.commit()
    r = compute_compare(db, today=jan)
    assert r["previous_label"].startswith("Dec")
    by = {c["id"]: c for c in r["categories"]}
    assert by["dining"]["current"] == 30.0 and by["dining"]["previous"] == 20.0
