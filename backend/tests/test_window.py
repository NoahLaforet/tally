"""Window math and dashboard aggregation.

These tests use a private in-memory engine so nothing here touches the shared
test database. compute_dashboard builds its window from date.today(), so every
synthetic date is derived relative to today.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import sqlalchemy.pool
import sqlmodel
from sqlmodel import Session, SQLModel

from app.main import _month_window, _period_label, compute_dashboard
from app.models import Account, Transaction
from app.seed import seed_cards


@pytest.fixture
def mem_session():
    # Private engine. Rows created here never reach the shared app DB.
    engine = sqlmodel.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# ───────────────────────── _month_window ─────────────────────────
def test_month_window_returns_months_plus_current():
    win = _month_window(6, today=date(2026, 3, 15))
    assert len(win) == 7
    assert win[-1] == (2026, 3)
    assert win == [(2025, 9), (2025, 10), (2025, 11), (2025, 12),
                   (2026, 1), (2026, 2), (2026, 3)]


def test_month_window_crosses_january_boundary():
    # A January today must walk back into the previous year.
    win = _month_window(6, today=date(2026, 1, 15))
    assert win == [(2025, 7), (2025, 8), (2025, 9), (2025, 10),
                   (2025, 11), (2025, 12), (2026, 1)]


def test_month_window_defaults_to_today():
    win = _month_window(6)
    today = date.today()
    assert len(win) == 7
    assert win[-1] == (today.year, today.month)


# ───────────────────────── _period_label ─────────────────────────
def test_period_label_same_year():
    win = _month_window(4, today=date(2026, 5, 15))
    assert _period_label(win) == "Jan-May 2026"


def test_period_label_cross_year():
    win = _month_window(6, today=date(2026, 3, 15))
    assert _period_label(win) == "Sep 2025 - Mar 2026"


# ───────────────────────── compute_dashboard ─────────────────────────
def _txn(uid: str, account_id: int, d: date, cents: int, raw: str,
         norm: str, cat: str, is_transfer: bool = False) -> Transaction:
    return Transaction(
        txn_uid=uid, account_id=account_id, posted_date=d, amount_cents=cents,
        raw_description=raw, norm_merchant=norm, category=cat,
        is_transfer=is_transfer,
    )


def test_compute_dashboard_window_buckets_and_average(mem_session):
    session = mem_session
    seed_cards(session)
    acc = Account(name="Apple Card", kind="credit", card_key="apple")
    session.add(acc)
    session.commit()
    session.refresh(acc)

    # The same window compute_dashboard will build internally.
    today = date.today()
    win = _month_window(6)
    window_start = date(win[0][0], win[0][1], 1)

    # Spend in three different full months of the window. Day 15 exists in
    # every month so these dates are valid on any run day.
    m_a, m_b, m_c = win[1], win[3], win[5]
    session.add(_txn("w1", acc.id, date(m_a[0], m_a[1], 15), -10050,
                     "TRADER JOES #071", "Trader Joe's", "grocery"))
    session.add(_txn("w2", acc.id, date(m_b[0], m_b[1], 15), -20075,
                     "CHIPOTLE 1234", "Chipotle", "dining"))
    session.add(_txn("w3", acc.id, date(m_c[0], m_c[1], 15), -30000,
                     "TARGET 00123", "Target", "shopping"))
    # One day before the window opens. Must be excluded by the date filter.
    session.add(_txn("w4", acc.id, window_start - timedelta(days=1), -99999,
                     "TARGET 00123", "Target", "shopping"))
    # First day of next month. Fetched by the query but its (year, month) is
    # not in the window index, so it must be dropped.
    ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    session.add(_txn("w5", acc.id, date(ny, nm, 1), -88888,
                     "CHIPOTLE 1234", "Chipotle", "dining"))
    # Transfer flagged row inside the window. The description is neutral on
    # purpose so only the is_transfer flag can exclude it.
    session.add(_txn("w6", acc.id, date(m_c[0], m_c[1], 16), -77777,
                     "STEAM PURCHASE 555", "Steam", "entertainment",
                     is_transfer=True))
    session.commit()

    data = compute_dashboard(session, months=6)

    # Only the three in-window purchases count: 100.50 + 200.75 + 300.00.
    assert data["spend"]["total6"] == 601.25

    # Trend buckets land in the right slots and nowhere else.
    trend = data["spend"]["trend"]
    assert len(trend) == 7
    assert trend[1] == 100.50
    assert trend[3] == 200.75
    assert trend[5] == 300.00
    for i in (0, 2, 4, 6):
        assert trend[i] == 0

    # The average divides by the 3 months that have data, not the 6 window
    # months, so a sparse database is not diluted toward zero.
    assert data["meta"]["full_months_with_data"] == 3
    assert data["spend"]["monthly_avg"] == round(60125 / 3 / 100, 2)

    assert data["meta"]["period"] == _period_label(win)
    assert len(data["meta"]["months"]) == 7
    assert data["meta"]["ocr_unreconciled"] == 0
    assert data["delivery"]["orders"] == 0
