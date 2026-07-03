"""Demo dataset: loads once into an empty ledger, refuses otherwise."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, func, select

from app.demo import load_demo
from app.models import BalanceSnapshot, Subscription, Transaction


@pytest.fixture
def db():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def test_demo_loads_into_empty_ledger(db):
    out = load_demo(db)
    assert out["ok"] and out["transactions"] > 100
    assert db.exec(select(func.count()).select_from(Transaction)).one() > 100
    assert db.exec(select(func.count()).select_from(BalanceSnapshot)).one() > 0
    assert db.exec(select(func.count()).select_from(Subscription)).one() == 4
    # Everything is clearly fake.
    accounts = {t.account_id for t in db.exec(select(Transaction)).all()}
    assert len(accounts) == 2


def test_demo_refuses_nonempty_ledger(db):
    load_demo(db)
    with pytest.raises(HTTPException) as e:
        load_demo(db)
    assert e.value.status_code == 409


def test_demo_is_deterministic(db):
    load_demo(db)
    uids1 = sorted(t.txn_uid for t in db.exec(select(Transaction)).all())

    engine2 = create_engine("sqlite://",
                            connect_args={"check_same_thread": False},
                            poolclass=StaticPool)
    SQLModel.metadata.create_all(engine2)
    with Session(engine2) as s2:
        load_demo(s2)
        uids2 = sorted(t.txn_uid for t in s2.exec(select(Transaction)).all())
    assert uids1 == uids2
