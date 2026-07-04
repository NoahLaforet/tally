"""Reimbursement marking and suggestion engine."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.canonical import make_txn_uid
from app.main import compute_dashboard
from app.models import Account, Transaction
from app.reimburse import find_suggestions
from app.seed import seed_cards


@pytest.fixture
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _txn(s, acct_id, posted, cents, desc, seq=0, **kw):
    t = Transaction(
        txn_uid=make_txn_uid(f"t:{acct_id}", posted, cents, desc, seq),
        account_id=acct_id, posted_date=posted, amount_cents=cents,
        raw_description=desc, norm_merchant=desc.upper(), **kw)
    s.add(t)
    return t


def test_single_exact_repayment_suggested(db):
    a = Account(name="Checking", kind="checking")
    db.add(a)
    db.commit()
    today = date.today()
    _txn(db, a.id, today - timedelta(days=10), -180000, "APARTMENT RENT")
    _txn(db, a.id, today - timedelta(days=8), 180000, "ZELLE FROM MOM")
    db.commit()
    out = find_suggestions(db)
    assert len(out) == 1
    assert out[0]["merchant"] == "APARTMENT RENT"
    assert len(out[0]["repaid_by"]) == 1
    assert out[0]["repaid_by"][0]["amount"] == 1800.0


def test_multi_leg_group_repayment(db):
    a = Account(name="Card", kind="credit")
    db.add(a)
    db.commit()
    today = date.today()
    _txn(db, a.id, today - timedelta(days=6), -30000, "GROUP DINNER")
    _txn(db, a.id, today - timedelta(days=5), 10000, "VENMO ALEX")
    _txn(db, a.id, today - timedelta(days=4), 20000, "VENMO SAM")
    db.commit()
    out = find_suggestions(db)
    assert len(out) == 1
    assert {l["merchant"] for l in out[0]["repaid_by"]} == {"VENMO ALEX", "VENMO SAM"}


def test_no_suggestion_for_partial_or_late(db):
    a = Account(name="Checking", kind="checking")
    db.add(a)
    db.commit()
    today = date.today()
    _txn(db, a.id, today - timedelta(days=40), -180000, "TUITION SPRING")
    _txn(db, a.id, today - timedelta(days=5), 180000, "ZELLE LATE")  # 35d later
    _txn(db, a.id, today - timedelta(days=10), -50000, "CONCERT TICKETS")
    _txn(db, a.id, today - timedelta(days=9), 20000, "VENMO PART")  # partial
    db.commit()
    assert find_suggestions(db) == []


def test_payroll_never_counts_as_repayment(db):
    a = Account(name="Checking", kind="checking")
    db.add(a)
    db.commit()
    today = date.today()
    _txn(db, a.id, today - timedelta(days=9), -320000, "RENT CHECK")
    _txn(db, a.id, today - timedelta(days=7), 320000, "EMPLOYER PAYROLL DEP")
    db.commit()
    assert find_suggestions(db) == []


def test_marked_rows_are_skipped(db):
    a = Account(name="Checking", kind="checking")
    db.add(a)
    db.commit()
    today = date.today()
    _txn(db, a.id, today - timedelta(days=10), -180000, "APARTMENT RENT",
         reimbursement="group")
    _txn(db, a.id, today - timedelta(days=8), 180000, "ZELLE FROM MOM")
    db.commit()
    assert find_suggestions(db) == []


def test_dashboard_excludes_reimbursed(db):
    seed_cards(db)
    a = Account(name="Checking", kind="checking", card_key="debit")
    db.add(a)
    db.commit()
    today = date.today()
    d = today - timedelta(days=15)
    _txn(db, a.id, d, -180000, "APARTMENT RENT", reimbursement="thirdparty")
    _txn(db, a.id, d, -5000, "TRADER JOES", category="grocery")
    db.commit()
    dash = compute_dashboard(db)
    assert dash["spend"]["total6"] == 50.0  # rent gone from spend
    assert dash["meta"]["reimbursed_excluded"]["count"] == 1
    assert dash["meta"]["reimbursed_excluded"]["total"] == 1800.0
