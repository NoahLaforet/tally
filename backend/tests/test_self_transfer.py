"""Single-leg self-transfers must not count as spending."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.canonical import make_txn_uid
from app.ingest.common import looks_like_self_transfer
from app.ingest.pipeline import _match_transfers
from app.main import compute_dashboard
from app.models import Account, Transaction
from app.seed import seed_cards


def test_classifier_hits_bank_phrasings():
    yes = [
        "Recurring Transfer To Laforet N Way2Save Sa",
        "Online Transfer Ref #Ib0Yjdnt8P To Wells",
        "ONLINE TRANSFER FROM LAFORET N SAVINGS",
        "Mobile Transfer to Checking x1234",
        "SAVINGS TRANSFER",
        "Transfer to Savings",
    ]
    no = [
        "Zelle To Alex Smith",
        "VENMO PAYMENT",
        "CREDIT CARD AUTO PAY",
        "WIRE TRANSFER FEE",       # not a to/from own-account phrasing
        "DOORDASH*BURGERS",
        "TRANSFERWISE INC",        # merchant name, not a transfer phrase
    ]
    for d in yes:
        assert looks_like_self_transfer(d), d
    for d in no:
        assert not looks_like_self_transfer(d), d


@pytest.fixture
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _txn(s, acct_id, posted, cents, desc, **kw):
    t = Transaction(
        txn_uid=make_txn_uid(f"t:{acct_id}", posted, cents, desc, 0),
        account_id=acct_id, posted_date=posted, amount_cents=cents,
        raw_description=desc, norm_merchant=desc.upper(), **kw)
    s.add(t)
    return t


def test_unpaired_self_transfer_gets_solo_flag_and_leaves_spend(db):
    seed_cards(db)
    a = Account(name="Checking", kind="checking", card_key="debit")
    db.add(a)
    db.commit()
    d = date.today() - timedelta(days=15)
    _txn(db, a.id, d, -87_00, "Recurring Transfer To Owner Way2Save Sa",
         category="other")
    _txn(db, a.id, d, -50_00, "TRADER JOES", category="grocery")
    db.commit()
    _match_transfers(db)

    rows = {t.raw_description: t for t in db.exec(select(Transaction)).all()}
    assert rows["Recurring Transfer To Owner Way2Save Sa"].is_transfer
    assert rows["Recurring Transfer To Owner Way2Save Sa"].transfer_group_id is None
    assert not rows["TRADER JOES"].is_transfer

    dash = compute_dashboard(db)
    assert dash["spend"]["total6"] == 50.0  # only the groceries


def test_solo_flag_still_pairs_when_other_leg_arrives(db):
    a = Account(name="Checking", kind="checking")
    b = Account(name="Savings", kind="savings")
    db.add(a)
    db.add(b)
    db.commit()
    d = date.today() - timedelta(days=10)
    out = _txn(db, a.id, d, -100_00, "Online Transfer Ref #AB12 To Savings")
    db.commit()
    _match_transfers(db)
    db.refresh(out)
    assert out.is_transfer and out.transfer_group_id is None

    _txn(db, b.id, d + timedelta(days=1), 100_00,
         "Online Transfer Ref #AB12 From Checking")
    db.commit()
    _match_transfers(db)
    db.refresh(out)
    assert out.is_transfer and out.transfer_group_id is not None
