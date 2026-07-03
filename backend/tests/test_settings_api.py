"""Tests for the server-side persistence API (app/api_settings.py).

Endpoint tests go through the shared conftest client and clean up every row
they create in a finally block, because the test database is shared process
wide. Pure math (forward fill, replace semantics, migration idempotency) runs
against a private in-memory engine.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
import sqlalchemy
from sqlmodel import Session, SQLModel, create_engine, select

from app.api_settings import (apply_budget_targets, apply_local_blob,
                              compute_networth)
from app.api_settings import router as settings_router
from app.db import engine as shared_engine
from app.main import app
from app.models import (Account, BalanceSnapshot, Budget, IncomeSource,
                        Setting, Transaction)

# The orchestrator wires this router into main.py later. Until then, attach
# it here so the shared client fixture can reach the endpoints. The guard
# keeps this a no-op once main.py includes the router itself.
if not any(getattr(r, "path", "") == "/api/savings-plan" for r in app.routes):
    app.include_router(settings_router)


@pytest.fixture
def mem_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _capture_setting(key: str) -> str | None:
    with Session(shared_engine) as s:
        row = s.get(Setting, key)
        return row.value_json if row else None


def _restore_setting(key: str, prior: str | None) -> None:
    with Session(shared_engine) as s:
        row = s.get(Setting, key)
        if prior is None:
            if row is not None:
                s.delete(row)
        else:
            if row is None:
                row = Setting(key=key, value_json=prior)
            else:
                row.value_json = prior
            s.add(row)
        s.commit()


def _delete_account_rows(names: list[str]) -> None:
    with Session(shared_engine) as s:
        for a in s.exec(select(Account).where(Account.name.in_(names))).all():
            for snap in s.exec(select(BalanceSnapshot)
                               .where(BalanceSnapshot.account_id == a.id)).all():
                s.delete(snap)
            s.flush()
            s.delete(a)
        s.commit()


# ---------- private-engine unit tests ----------

def test_networth_forward_fill(mem_session):
    s = mem_session
    today = date.today()
    d1, d2, d3 = today - timedelta(days=30), today - timedelta(days=20), today - timedelta(days=10)

    a = Account(name="A Sav", kind="savings", balance_cents=15000)
    b = Account(name="B Chk", kind="checking", balance_cents=5000)
    c = Account(name="C Manual", kind="invest", balance_cents=2500)
    s.add(a)
    s.add(b)
    s.add(c)
    s.flush()
    s.add(BalanceSnapshot(account_id=a.id, taken_on=d1, balance_cents=10000))
    s.add(BalanceSnapshot(account_id=a.id, taken_on=d3, balance_cents=15000))
    s.add(BalanceSnapshot(account_id=b.id, taken_on=d2, balance_cents=5000))
    s.commit()

    out = compute_networth(s, today=today)

    # d1: only A known. d2: A carried forward plus B. d3: A updated, B carried.
    # today: synthetic snapshot for C, which has no persisted snapshots.
    assert [p["date"] for p in out["series"]] == [
        d1.isoformat(), d2.isoformat(), d3.isoformat(), today.isoformat()]
    assert [p["total"] for p in out["series"]] == [100.0, 150.0, 200.0, 225.0]

    latest = {r["id"]: r["latest"] for r in out["accounts"]}
    assert latest[a.id] == 150.0
    assert latest[b.id] == 50.0
    assert latest[c.id] == 25.0

    # The synthetic snapshot for C must not be persisted.
    persisted = s.exec(select(BalanceSnapshot)
                       .where(BalanceSnapshot.account_id == c.id)).all()
    assert persisted == []


def test_networth_zero_balance_account_not_synthesized(mem_session):
    s = mem_session
    a = Account(name="Empty", kind="checking", balance_cents=0)
    s.add(a)
    s.commit()
    out = compute_networth(s)
    assert out["series"] == []
    assert out["accounts"][0]["latest"] == 0.0


def test_budget_replace_semantics(mem_session):
    s = mem_session
    apply_budget_targets(s, {"dining": 10, "gas": 20}, replace=False)
    s.commit()
    res = apply_budget_targets(s, {"dining": 15.5}, replace=True)
    s.commit()
    assert res == {"upserted": 1, "deleted": 1}
    rows = {b.category: b.target_cents for b in s.exec(select(Budget)).all()}
    assert rows == {"dining": 1550}


def test_apply_local_blob_idempotent(mem_session):
    s = mem_session
    blob = {
        "income": [{"name": "Primary income", "amount": 3210.55},
                   {"name": "Other income", "amount": 0}],
        "incomeNote": "ui copy, not migrated",
        "accounts": [{"name": "Checking", "balance": 1200.10, "apy": 0.01, "kind": "checking"},
                     {"name": "High-yield savings", "balance": 9000, "apy": 3.40, "kind": "savings"}],
        "savings": {"monthly": 500, "goal": 10000, "autoInto": "High-yield savings"},
        "moves": {"Netflix": True},
        "targets": {"dining": 600, "grocery": 325.25},
    }
    first = apply_local_blob(s, blob)
    s.commit()
    assert first["budgets"] == 2
    assert first["income"] == 2
    assert first["accounts_created"] == 2
    assert first["accounts_updated"] == 0
    assert first["savings"] == 1

    second = apply_local_blob(s, blob)
    s.commit()
    assert second["accounts_created"] == 0
    assert second["accounts_updated"] == 2

    assert len(s.exec(select(IncomeSource)).all()) == 2
    assert len(s.exec(select(Account)).all()) == 2
    assert len(s.exec(select(Budget)).all()) == 2
    # One snapshot per account per day, so a second run adds nothing.
    assert len(s.exec(select(BalanceSnapshot)).all()) == 2

    inc = {r.name: r.amount_cents for r in s.exec(select(IncomeSource)).all()}
    assert inc["Primary income"] == 321055
    hys = s.exec(select(Account)
                 .where(Account.name == "High-yield savings")).one()
    assert hys.balance_cents == 900000
    assert hys.apy_bps == 340
    assert hys.is_manual is True
    tgt = {b.category: b.target_cents for b in s.exec(select(Budget)).all()}
    assert tgt == {"dining": 60000, "grocery": 32525}

    plan = s.get(Setting, "savings_plan")
    assert plan is not None
    assert '"monthly_cents": 50000' in plan.value_json
    assert '"goal_cents": 1000000' in plan.value_json


def test_apply_local_blob_matches_accounts_case_insensitively(mem_session):
    s = mem_session
    s.add(Account(name="Apple Card", kind="credit", is_manual=False,
                  balance_cents=0, apy_bps=0))
    s.commit()
    counts = apply_local_blob(
        s, {"accounts": [{"name": "apple card", "balance": 42.42,
                          "apy": 0, "kind": "credit"}]})
    s.commit()
    assert counts["accounts_created"] == 0
    assert counts["accounts_updated"] == 1
    acct = s.exec(select(Account)).one()
    assert acct.balance_cents == 4242


# ---------- endpoint tests through the shared client ----------

def test_budgets_endpoints(client):
    cat = "zz_test_budget_cat"
    try:
        r = client.put("/api/budgets", json={"targets": {cat: 123.45}})
        assert r.status_code == 200
        assert r.json()["upserted"] == 1

        r = client.get("/api/budgets")
        assert r.status_code == 200
        mine = [b for b in r.json() if b["category"] == cat]
        assert mine == [{"category": cat, "target": 123.45}]

        # Upsert overwrites the same category, no duplicate rows.
        client.put("/api/budgets", json={"targets": {cat: 200}})
        mine = [b for b in client.get("/api/budgets").json()
                if b["category"] == cat]
        assert mine == [{"category": cat, "target": 200.0}]
    finally:
        with Session(shared_engine) as s:
            row = s.get(Budget, cat)
            if row is not None:
                s.delete(row)
                s.commit()


def test_income_crud(client):
    name = "ZZ Test Income"
    try:
        r = client.post("/api/income", json={"name": name, "amount": 1234.56})
        assert r.status_code == 201
        row = r.json()
        assert row["name"] == name
        assert row["amount"] == 1234.56
        iid = row["id"]

        assert any(i["id"] == iid for i in client.get("/api/income").json())

        r = client.patch(f"/api/income/{iid}", json={"amount": 2000})
        assert r.status_code == 200
        assert r.json()["amount"] == 2000.0

        r = client.delete(f"/api/income/{iid}")
        assert r.status_code == 200
        assert not any(i["id"] == iid for i in client.get("/api/income").json())
        assert client.delete(f"/api/income/{iid}").status_code == 404
    finally:
        with Session(shared_engine) as s:
            for row in s.exec(select(IncomeSource)
                              .where(IncomeSource.name == name)).all():
                s.delete(row)
            s.commit()


def test_accounts_crud_and_snapshots(client):
    name = "ZZ Test Checking"
    try:
        r = client.post("/api/accounts", json={
            "name": name, "kind": "checking", "balance": 100.5, "apy": 3.4})
        assert r.status_code == 201
        acct = r.json()
        aid = acct["id"]
        assert acct["balance"] == 100.5
        assert acct["apy"] == 3.4
        assert acct["is_manual"] is True

        with Session(shared_engine) as s:
            snaps = s.exec(select(BalanceSnapshot)
                           .where(BalanceSnapshot.account_id == aid)).all()
            assert len(snaps) == 1
            assert snaps[0].balance_cents == 10050
            assert snaps[0].taken_on == date.today()

        # A balance change upserts today's snapshot instead of adding a row.
        r = client.patch(f"/api/accounts/{aid}", json={"balance": 200})
        assert r.status_code == 200
        assert r.json()["balance"] == 200.0
        with Session(shared_engine) as s:
            snaps = s.exec(select(BalanceSnapshot)
                           .where(BalanceSnapshot.account_id == aid)).all()
            assert len(snaps) == 1
            assert snaps[0].balance_cents == 20000

        r = client.patch(f"/api/accounts/{aid}",
                         json={"name": "ZZ Test Checking", "apy": 4.15,
                               "card_key": "wf_autograph"})
        assert r.json()["apy"] == 4.15
        assert r.json()["card_key"] == "wf_autograph"

        # The new account shows up in the net worth payload.
        nw = client.get("/api/networth").json()
        mine = [a for a in nw["accounts"] if a["id"] == aid]
        assert mine and mine[0]["latest"] == 200.0
        assert any(p["date"] == date.today().isoformat()
                   for p in nw["series"])

        r = client.delete(f"/api/accounts/{aid}")
        assert r.status_code == 200
        with Session(shared_engine) as s:
            assert s.get(Account, aid) is None
            assert s.exec(select(BalanceSnapshot)
                          .where(BalanceSnapshot.account_id == aid)).all() == []
        assert client.delete(f"/api/accounts/{aid}").status_code == 404
    finally:
        _delete_account_rows([name])


def test_account_delete_guards(client):
    manual_name = "ZZ Guard Manual"
    linked_name = "ZZ Guard Linked"
    txn_uid = "zz-test-settings-guard-txn"
    try:
        r = client.post("/api/accounts", json={"name": manual_name,
                                               "kind": "checking"})
        aid = r.json()["id"]
        with Session(shared_engine) as s:
            s.add(Transaction(
                txn_uid=txn_uid, account_id=aid,
                posted_date=date.today(), amount_cents=-500,
                raw_description="zz guard txn", norm_merchant="zz guard"))
            linked = Account(name=linked_name, kind="checking",
                             is_manual=False)
            s.add(linked)
            s.commit()
            s.refresh(linked)
            linked_id = linked.id

        # An account with transactions must not be deletable.
        assert client.delete(f"/api/accounts/{aid}").status_code == 409
        # A non-manual (Plaid linked) account must not be deletable.
        assert client.delete(f"/api/accounts/{linked_id}").status_code == 409

        with Session(shared_engine) as s:
            s.delete(s.get(Transaction, txn_uid))
            s.commit()
        assert client.delete(f"/api/accounts/{aid}").status_code == 200
    finally:
        with Session(shared_engine) as s:
            txn = s.get(Transaction, txn_uid)
            if txn is not None:
                s.delete(txn)
            s.commit()
        _delete_account_rows([manual_name, linked_name])


def test_savings_plan_endpoints(client):
    prior = _capture_setting("savings_plan")
    try:
        r = client.get("/api/savings-plan")
        assert r.status_code == 200
        assert set(r.json()) == {"monthly", "goal", "note"}

        r = client.put("/api/savings-plan",
                       json={"monthly": 500, "goal": 10000, "note": "zz test"})
        assert r.status_code == 200
        assert r.json() == {"monthly": 500.0, "goal": 10000.0, "note": "zz test"}

        # A partial update leaves the other fields alone.
        r = client.put("/api/savings-plan", json={"monthly": 600.25})
        assert r.json() == {"monthly": 600.25, "goal": 10000.0, "note": "zz test"}
        assert client.get("/api/savings-plan").json()["monthly"] == 600.25
    finally:
        _restore_setting("savings_plan", prior)


def test_migrate_local_endpoint_idempotent(client):
    prior_plan = _capture_setting("savings_plan")
    acct_name = "ZZ Migrate Savings"
    income_name = "ZZ Migrate Income"
    cat = "zz_migrate_cat"
    blob = {
        "income": [{"name": income_name, "amount": 4321}],
        "incomeNote": "ignored",
        "accounts": [{"name": acct_name, "balance": 777.77, "apy": 4.0,
                      "kind": "savings"}],
        "savings": {"monthly": 250, "goal": 5000, "autoInto": acct_name},
        "moves": {},
        "targets": {cat: 55.5},
    }
    try:
        r = client.post("/api/migrate-local", json=blob)
        assert r.status_code == 200
        first = r.json()
        assert first["ok"] is True
        assert first["budgets"] == 1
        assert first["income"] == 1
        assert first["accounts_created"] == 1
        assert first["savings"] == 1

        r = client.post("/api/migrate-local", json=blob)
        second = r.json()
        assert second["accounts_created"] == 0
        assert second["accounts_updated"] == 1

        with Session(shared_engine) as s:
            incomes = s.exec(select(IncomeSource)
                             .where(IncomeSource.name == income_name)).all()
            assert len(incomes) == 1
            assert incomes[0].amount_cents == 432100
            accts = s.exec(select(Account)
                           .where(Account.name == acct_name)).all()
            assert len(accts) == 1
            assert accts[0].balance_cents == 77777
            assert accts[0].apy_bps == 400
            assert s.get(Budget, cat).target_cents == 5550

        assert client.get("/api/savings-plan").json()["goal"] == 5000.0
    finally:
        with Session(shared_engine) as s:
            row = s.get(Budget, cat)
            if row is not None:
                s.delete(row)
            for r_ in s.exec(select(IncomeSource)
                             .where(IncomeSource.name == income_name)).all():
                s.delete(r_)
            s.commit()
        _delete_account_rows([acct_name])
        _restore_setting("savings_plan", prior_plan)
