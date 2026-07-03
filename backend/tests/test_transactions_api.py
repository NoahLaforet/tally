"""Transactions API: filters, pagination, patch/learn, exports.

Endpoint tests mount the transactions router on a private FastAPI app. The
router is not registered on app.main yet, but it reads the same shared test
database through app.db.engine, so every row created here uses a unique
merchant prefix and is deleted in the fixture teardown. Nothing here asserts
on global counts of the shared database.

The helper-level test uses a fully private in-memory engine instead.
"""

from __future__ import annotations

import csv
import uuid
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api_transactions import (COLUMNS, _filters, apply_patch,
                                  list_transactions, router)
from app.db import engine, init_db
from app.models import Account, LearnedCategory, Transaction


@pytest.fixture(scope="module")
def api():
    """A private app carrying only the transactions router (auth is off)."""
    init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _txn(uid: str, acct_id: int, d: date, cents: int, merchant: str,
         desc: str, cat: str, origin: str = "statement",
         locked: bool = False) -> Transaction:
    return Transaction(txn_uid=uid, account_id=acct_id, posted_date=d,
                       amount_cents=cents, raw_description=desc,
                       norm_merchant=merchant, category=cat,
                       category_source="rule", origin=origin,
                       user_locked=locked)


@pytest.fixture
def seeded():
    """A dedicated account plus 8 transactions under a unique merchant prefix."""
    init_db()
    pfx = f"txapi{uuid.uuid4().hex[:10]}"
    coffee = f"{pfx} coffee shop"
    grocer = f"{pfx} grocer"
    zeta = f"{pfx} zeta fuel"
    desc_token = f"{pfx}zdesc"
    uids = {f"u{i}": f"{pfx}-u{i}" for i in range(1, 9)}

    with Session(engine) as s:
        acct = Account(name=f"{pfx} account", kind="credit")
        s.add(acct)
        s.commit()
        s.refresh(acct)
        acct_id = acct.id
        rows = [
            _txn(uids["u1"], acct_id, date(2026, 1, 10), -500, coffee,
                 f"{pfx.upper()} COFFEE SHOP #1", "other"),
            _txn(uids["u2"], acct_id, date(2026, 1, 11), -650, coffee,
                 f"{pfx.upper()} COFFEE SHOP #2", "other"),
            _txn(uids["u3"], acct_id, date(2026, 1, 12), -700, coffee,
                 f"{pfx.upper()} COFFEE SHOP #3", "dining", locked=True),
            _txn(uids["u4"], acct_id, date(2026, 1, 13), -1000, grocer,
                 f"{pfx.upper()} GROCER 11", "grocery"),
            _txn(uids["u5"], acct_id, date(2026, 1, 14), -1150, grocer,
                 f"{pfx.upper()} GROCER 12", "grocery"),
            _txn(uids["u6"], acct_id, date(2026, 2, 1), -1200, zeta,
                 f"ZETA FUEL {desc_token} PUMP 4", "gas", origin="plaid"),
            _txn(uids["u7"], acct_id, date(2026, 2, 2), 2500, zeta,
                 f"{pfx.upper()} ZETA FUEL REFUND", "other"),
            _txn(uids["u8"], acct_id, date(2026, 2, 3), -1300, zeta,
                 f"{pfx.upper()} ZETA FUEL 88", "gas", origin="ocr"),
        ]
        for r in rows:
            s.add(r)
        s.commit()

    data = {"pfx": pfx, "acct_id": acct_id, "uids": uids, "coffee": coffee,
            "grocer": grocer, "zeta": zeta, "desc_token": desc_token}
    try:
        yield data
    finally:
        # The test database is shared process wide; remove every row we made.
        with Session(engine) as s:
            for uid in uids.values():
                t = s.get(Transaction, uid)
                if t is not None:
                    s.delete(t)
            learned = s.exec(select(LearnedCategory).where(
                LearnedCategory.norm_merchant.like(f"{pfx}%"))).all()
            for lc in learned:
                s.delete(lc)
            a = s.get(Account, acct_id)
            if a is not None:
                s.delete(a)
            s.commit()


def test_list_shape_and_sort(api, seeded):
    r = api.get("/api/transactions", params={"q": seeded["pfx"]})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 8
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert len(body["rows"]) == 8
    # Newest date first, then txn_uid.
    assert [row["uid"] for row in body["rows"]][:3] == [
        seeded["uids"]["u8"], seeded["uids"]["u7"], seeded["uids"]["u6"]]
    assert body["rows"][-1]["uid"] == seeded["uids"]["u1"]
    top = body["rows"][0]
    assert set(top.keys()) == set(COLUMNS)
    assert top["date"] == "2026-02-03"
    assert top["amount"] == -13.0
    assert top["amount_cents"] == -1300
    assert top["merchant"] == seeded["zeta"]
    assert top["account_id"] == seeded["acct_id"]
    assert top["account"] == f"{seeded['pfx']} account"
    assert top["origin"] == "ocr"
    assert top["is_transfer"] is False
    assert top["user_locked"] is False


def test_list_filters(api, seeded):
    pfx = seeded["pfx"]

    # Case-insensitive substring over both merchant and description.
    r = api.get("/api/transactions", params={"q": pfx.upper()})
    assert r.json()["total"] == 8

    # This token only exists in one raw_description, never in a merchant.
    r = api.get("/api/transactions", params={"q": seeded["desc_token"]})
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["uid"] == seeded["uids"]["u6"]

    r = api.get("/api/transactions",
                params={"q": pfx, "category": "grocery"})
    assert r.json()["total"] == 2

    r = api.get("/api/transactions", params={"account_id": seeded["acct_id"]})
    assert r.json()["total"] == 8

    r = api.get("/api/transactions", params={"q": pfx, "origin": "plaid"})
    body = r.json()
    assert body["total"] == 1
    assert body["rows"][0]["uid"] == seeded["uids"]["u6"]

    r = api.get("/api/transactions",
                params={"q": pfx, "date_from": "2026-02-01"})
    assert r.json()["total"] == 3

    r = api.get("/api/transactions",
                params={"q": pfx, "date_to": "2026-01-14"})
    assert r.json()["total"] == 5

    r = api.get("/api/transactions",
                params={"q": pfx, "date_from": "2026-01-11",
                        "date_to": "2026-01-13"})
    assert r.json()["total"] == 3


def test_pagination(api, seeded):
    pfx = seeded["pfx"]
    seen: list[str] = []
    for page, expect in ((1, 3), (2, 3), (3, 2)):
        r = api.get("/api/transactions",
                    params={"q": pfx, "page": page, "page_size": 3})
        body = r.json()
        assert body["total"] == 8
        assert body["page"] == page
        assert body["page_size"] == 3
        assert len(body["rows"]) == expect
        seen += [row["uid"] for row in body["rows"]]
    assert len(seen) == len(set(seen)) == 8

    r = api.get("/api/transactions",
                params={"q": pfx, "page": 4, "page_size": 3})
    assert r.json()["rows"] == []

    # page_size is clamped to the max, page to at least 1.
    r = api.get("/api/transactions",
                params={"q": pfx, "page": 0, "page_size": 500})
    body = r.json()
    assert body["page"] == 1
    assert body["page_size"] == 200


def test_patch_learns_and_bulk_applies(api, seeded):
    uids = seeded["uids"]
    r = api.patch(f"/api/transactions/{uids['u1']}",
                  json={"category": "roastery"})
    assert r.status_code == 200
    # u2 shares the merchant and is unlocked; u3 is locked and skipped.
    assert r.json() == {"ok": True, "updated_others": 1}

    with Session(engine) as s:
        u1 = s.get(Transaction, uids["u1"])
        assert u1.category == "roastery"
        assert u1.category_source == "manual"
        assert u1.user_locked is True
        u2 = s.get(Transaction, uids["u2"])
        assert u2.category == "roastery"
        assert u2.category_source == "learned"
        assert u2.user_locked is False
        u3 = s.get(Transaction, uids["u3"])
        assert u3.category == "dining"
        assert u3.category_source == "rule"
        lc = s.get(LearnedCategory, seeded["coffee"])
        assert lc is not None
        assert lc.category == "roastery"

    # A second recategorize upserts the learned row and skips both locked rows.
    r = api.patch(f"/api/transactions/{uids['u2']}",
                  json={"category": "brunch"})
    assert r.json() == {"ok": True, "updated_others": 0}
    with Session(engine) as s:
        assert s.get(LearnedCategory, seeded["coffee"]).category == "brunch"
        assert s.get(Transaction, uids["u1"]).category == "roastery"

    # Whitespace-only category is rejected.
    r = api.patch(f"/api/transactions/{uids['u1']}", json={"category": "  "})
    assert r.status_code == 422


def test_patch_explicit_unlock_and_toggle(api, seeded):
    uids = seeded["uids"]

    # category plus an explicit user_locked=false leaves the row unlocked.
    r = api.patch(f"/api/transactions/{uids['u4']}",
                  json={"category": "veggies", "user_locked": False})
    assert r.json() == {"ok": True, "updated_others": 1}
    with Session(engine) as s:
        u4 = s.get(Transaction, uids["u4"])
        assert u4.category == "veggies"
        assert u4.category_source == "manual"
        assert u4.user_locked is False
        assert s.get(Transaction, uids["u5"]).category == "veggies"

    # Recategorize locks by default, then user_locked=false alone unlocks.
    r = api.patch(f"/api/transactions/{uids['u1']}",
                  json={"category": "espresso"})
    assert r.status_code == 200
    with Session(engine) as s:
        assert s.get(Transaction, uids["u1"]).user_locked is True

    r = api.patch(f"/api/transactions/{uids['u1']}",
                  json={"user_locked": False})
    assert r.json() == {"ok": True, "updated_others": 0}
    with Session(engine) as s:
        u1 = s.get(Transaction, uids["u1"])
        assert u1.user_locked is False
        # The lock toggle does not touch the category fields.
        assert u1.category == "espresso"
        assert u1.category_source == "manual"


def test_patch_unknown_uid_404(api):
    r = api.patch("/api/transactions/does-not-exist-xyz",
                  json={"category": "x"})
    assert r.status_code == 404


def test_export_csv(api, seeded):
    pfx = seeded["pfx"]
    r = api.get("/api/transactions/export.csv", params={"q": pfx})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "tally-transactions.csv" in r.headers["content-disposition"]
    rows = list(csv.reader(r.text.splitlines()))
    assert rows[0] == COLUMNS
    assert len(rows) == 9
    assert {row[0] for row in rows[1:]} == set(seeded["uids"].values())

    # Filters apply to exports too, with no pagination.
    r = api.get("/api/transactions/export.csv",
                params={"q": pfx, "category": "gas"})
    rows = list(csv.reader(r.text.splitlines()))
    assert len(rows) == 3


def test_export_json(api, seeded):
    r = api.get("/api/transactions/export.json", params={"q": seeded["pfx"]})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 8
    assert body[0]["uid"] == seeded["uids"]["u8"]
    for row in body:
        assert set(row.keys()) == set(COLUMNS)
    by_uid = {row["uid"]: row for row in body}
    assert by_uid[seeded["uids"]["u1"]]["amount"] == -5.0
    assert by_uid[seeded["uids"]["u1"]]["amount_cents"] == -500


def test_helpers_on_private_engine():
    # Pure logic test on a throwaway in-memory database.
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        acct = Account(name="unit acct", kind="credit")
        s.add(acct)
        s.commit()
        s.refresh(acct)
        s.add(_txn("unit-1", acct.id, date(2026, 3, 1), -1234,
                   "unit cafe", "UNIT CAFE 001", "other"))
        s.add(_txn("unit-2", acct.id, date(2026, 3, 2), -1500,
                   "unit cafe", "UNIT CAFE 002", "other"))
        s.add(_txn("unit-3", acct.id, date(2026, 3, 3), -99,
                   "unit cafe", "UNIT CAFE 003", "dining", locked=True))
        s.commit()

        page = list_transactions(s, _filters(q="UNIT CAFE"))
        assert page["total"] == 3
        assert [r["uid"] for r in page["rows"]] == ["unit-3", "unit-2", "unit-1"]
        assert page["rows"][0]["amount"] == -0.99
        assert page["rows"][0]["account"] == "unit acct"

        out = apply_patch(s, "unit-1", category="coffee", user_locked=None)
        assert out == {"ok": True, "updated_others": 1}
        assert s.get(Transaction, "unit-2").category == "coffee"
        assert s.get(Transaction, "unit-2").category_source == "learned"
        assert s.get(Transaction, "unit-3").category == "dining"
        assert s.get(LearnedCategory, "unit cafe").category == "coffee"

        assert apply_patch(s, "missing", category="x", user_locked=None) is None
