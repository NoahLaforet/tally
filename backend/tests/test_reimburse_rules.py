"""Reimbursement auto-rules: mark once, excluded forever."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api_transactions import apply_patch
from app.canonical import make_txn_uid
from app.ingest.convergence import reimbursement_rule
from app.models import Account, ReimbursementRule, Transaction


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
        txn_uid=make_txn_uid(f"r:{acct_id}", posted, cents, desc, seq),
        account_id=acct_id, posted_date=posted, amount_cents=cents,
        raw_description=desc, norm_merchant=desc.upper(), **kw)
    s.add(t)
    return t


def test_mark_with_apply_creates_rule_and_covers_siblings(db):
    a = Account(name="Savings", kind="savings")
    db.add(a)
    db.commit()
    d = date.today()
    rent1 = _txn(db, a.id, d - timedelta(days=40), -134383, "Manresa Properti WEB PMTS")
    rent2 = _txn(db, a.id, d - timedelta(days=10), -134383, "Manresa Properti WEB PMTS", seq=1)
    other = _txn(db, a.id, d - timedelta(days=5), -900, "TRADER JOES")
    db.commit()

    out = apply_patch(db, rent1.txn_uid, None, None,
                      reimbursement="thirdparty", apply_to_merchant=True)
    assert out["updated_others"] == 1

    db.refresh(rent2)
    db.refresh(other)
    assert rent2.reimbursement == "thirdparty"
    assert other.reimbursement is None
    assert reimbursement_rule(db, "MANRESA PROPERTI WEB PMTS") == "thirdparty"


def test_rule_applies_to_future_ingest(db):
    db.add(ReimbursementRule(norm_merchant="MANRESA PROPERTI WEB PMTS",
                             kind="thirdparty"))
    db.commit()
    # The ingest paths consult reimbursement_rule at insert; simulate the
    # lookup contract they use.
    assert reimbursement_rule(db, "MANRESA PROPERTI WEB PMTS") == "thirdparty"
    assert reimbursement_rule(db, "TRADER JOES") is None


def test_mark_without_apply_is_single_charge(db):
    a = Account(name="Card", kind="credit")
    db.add(a)
    db.commit()
    d = date.today()
    t1 = _txn(db, a.id, d - timedelta(days=9), -30000, "GROUP DINNER")
    t2 = _txn(db, a.id, d - timedelta(days=2), -28000, "GROUP DINNER", seq=1)
    db.commit()
    apply_patch(db, t1.txn_uid, None, None,
                reimbursement="group", apply_to_merchant=False)
    db.refresh(t2)
    assert t2.reimbursement is None
    assert db.get(ReimbursementRule, "GROUP DINNER") is None


def test_clear_with_apply_drops_rule_and_unmarks_all(db):
    a = Account(name="Savings", kind="savings")
    db.add(a)
    db.commit()
    d = date.today()
    r1 = _txn(db, a.id, d - timedelta(days=40), -134383, "Manresa Properti WEB PMTS",
              reimbursement="thirdparty")
    r2 = _txn(db, a.id, d - timedelta(days=10), -134383, "Manresa Properti WEB PMTS",
              seq=1, reimbursement="thirdparty")
    db.add(ReimbursementRule(norm_merchant="MANRESA PROPERTI WEB PMTS",
                             kind="thirdparty"))
    db.commit()

    out = apply_patch(db, r1.txn_uid, None, None,
                      clear_reimbursement=True, apply_to_merchant=True)
    assert out["updated_others"] == 1
    db.refresh(r2)
    assert r2.reimbursement is None
    assert db.get(ReimbursementRule, "MANRESA PROPERTI WEB PMTS") is None
