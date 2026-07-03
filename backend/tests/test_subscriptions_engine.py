"""Tests for the subscription detection engine.

Detector tests run on a private in-memory engine so nothing touches the
shared test DB. Endpoint tests go through HTTP against the shared engine;
the router is not registered on app.main until the orchestrator wires it,
so they mount the router on a scratch FastAPI app. Every row an endpoint
test creates is deleted in a finally block.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select

from app.db import engine as shared_engine
from app.models import Account, Card, Subscription, Transaction
from app.subscriptions_engine import detect_subscriptions, router

TODAY = date.today()


def make_session() -> Session:
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=sqlalchemy.pool.StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def add_txn(session: Session, merchant: str, day: date, cents: int,
            category: str = "subscriptions",
            account_id: int | None = None) -> None:
    """Insert one outflow. cents is the positive charge size."""
    session.add(Transaction(
        txn_uid=f"{merchant}-{day.isoformat()}-{cents}",
        account_id=account_id,
        posted_date=day,
        amount_cents=-cents,
        raw_description=merchant.upper(),
        norm_merchant=merchant,
        category=category,
    ))


def seed_cards(session: Session) -> None:
    session.add(Card(key="wf_autograph", name="Wells Fargo Autograph",
                     rules_json=json.dumps({"streaming": 300, "other": 100})))
    session.add(Card(key="apple", name="Apple Card",
                     rules_json=json.dumps({"streaming": 100, "other": 100})))
    session.commit()


# Detector unit tests on a private engine.

def test_monthly_detected_from_three_charges():
    with make_session() as s:
        seed_cards(s)
        acct = Account(name="WF Autograph", kind="credit",
                       card_key="wf_autograph")
        s.add(acct)
        s.commit()
        s.refresh(acct)
        d0 = TODAY - timedelta(days=61)
        add_txn(s, "netflix", d0, 1599, "streaming", acct.id)
        add_txn(s, "netflix", d0 + timedelta(days=30), 1599, "streaming", acct.id)
        add_txn(s, "netflix", d0 + timedelta(days=61), 1599, "streaming", acct.id)
        s.commit()

        summary = detect_subscriptions(s)

        assert summary == {"detected": 1, "updated": 0,
                           "flagged_creep": 0, "flagged_forgotten": 0}
        row = s.exec(select(Subscription)).one()
        assert row.name == "netflix"
        assert row.cadence_days == 30
        assert row.monthly_cents == 1599
        assert row.last_amount_cents == 1599
        assert row.last_seen_on == TODAY
        assert row.status == "review"
        assert row.detected is True
        assert row.flag is None
        assert row.current_card == "wf_autograph"
        # streaming pays 300 bps on the Autograph vs 100 on Apple Card
        assert row.recommended_card == "wf_autograph"
        assert row.norm_merchant == "netflix"


def test_weekly_detected_and_normalized():
    with make_session() as s:
        for k in (14, 7, 0):
            add_txn(s, "dashpass", TODAY - timedelta(days=k), 2500)
        s.commit()

        summary = detect_subscriptions(s)

        assert summary["detected"] == 1
        row = s.exec(select(Subscription)).one()
        assert row.cadence_days == 7
        # 2500 x 4.33 = 10825, integer math
        assert row.monthly_cents == 10825


def test_yearly_detected_and_normalized():
    with make_session() as s:
        for k in (730, 365, 0):
            add_txn(s, "amazon prime", TODAY - timedelta(days=k), 11999)
        s.commit()

        summary = detect_subscriptions(s)

        assert summary["detected"] == 1
        row = s.exec(select(Subscription)).one()
        assert row.cadence_days == 365
        # round(11999 / 12) = 1000
        assert row.monthly_cents == 1000


def test_price_creep_flag():
    with make_session() as s:
        add_txn(s, "hulu", TODAY - timedelta(days=60), 999)
        add_txn(s, "hulu", TODAY - timedelta(days=30), 999)
        add_txn(s, "hulu", TODAY, 1099)
        s.commit()

        summary = detect_subscriptions(s)

        assert summary["flagged_creep"] == 1
        row = s.exec(select(Subscription)).one()
        assert row.flag == "price_creep"
        assert row.monthly_cents == 1099
        assert row.last_amount_cents == 1099


def test_forgotten_flag():
    with make_session() as s:
        add_txn(s, "old gym app", TODAY - timedelta(days=200), 799)
        add_txn(s, "old gym app", TODAY - timedelta(days=170), 799)
        add_txn(s, "old gym app", TODAY - timedelta(days=140), 799)
        s.commit()

        summary = detect_subscriptions(s)

        assert summary["flagged_forgotten"] == 1
        row = s.exec(select(Subscription)).one()
        assert row.flag == "forgotten"
        assert row.cadence_days == 30


def test_variable_dining_merchant_not_detected():
    with make_session() as s:
        # Regular cadence but variable amounts; dining requires 2 percent
        # stability so DoorDash orders never read as a subscription.
        add_txn(s, "doordash", TODAY - timedelta(days=60), 2450, "dining")
        add_txn(s, "doordash", TODAY - timedelta(days=30), 2890, "dining")
        add_txn(s, "doordash", TODAY, 3125, "dining")
        s.commit()

        summary = detect_subscriptions(s)

        assert summary["detected"] == 0
        assert s.exec(select(Subscription)).first() is None


def test_touched_row_keeps_user_fields_but_measurements_update():
    with make_session() as s:
        seed_cards(s)
        s.add(Subscription(name="Spotify", monthly_cents=1099,
                           category="streaming", status="keep", moved=True,
                           current_card="apple",
                           manage_url="https://spotify.com/account",
                           norm_merchant="spotify", detected=False))
        s.commit()
        for k in (60, 30, 0):
            add_txn(s, "spotify", TODAY - timedelta(days=k), 1199, "streaming")
        s.commit()

        summary = detect_subscriptions(s)

        assert summary == {"detected": 0, "updated": 1,
                           "flagged_creep": 0, "flagged_forgotten": 0}
        row = s.exec(select(Subscription)).one()
        # user fields untouched
        assert row.status == "keep"
        assert row.moved is True
        assert row.current_card == "apple"
        assert row.manage_url == "https://spotify.com/account"
        # measurements updated
        assert row.monthly_cents == 1199
        assert row.cadence_days == 30
        assert row.last_amount_cents == 1199
        assert row.last_seen_on == TODAY


def test_name_containment_merges_into_seeded_row():
    with make_session() as s:
        # Seeded row has a display name and no norm_merchant yet.
        s.add(Subscription(name="Disney Plus", monthly_cents=1399,
                           category="streaming", status="review"))
        s.commit()
        for k in (61, 30, 0):
            add_txn(s, "disney plus bundle", TODAY - timedelta(days=k), 1499,
                    "streaming")
        s.commit()

        summary = detect_subscriptions(s)

        assert summary["detected"] == 0
        assert summary["updated"] == 1
        row = s.exec(select(Subscription)).one()
        assert row.name == "Disney Plus"
        assert row.norm_merchant == "disney plus bundle"
        assert row.monthly_cents == 1499


def test_rerun_is_idempotent():
    with make_session() as s:
        for k in (61, 30, 0):
            add_txn(s, "netflix", TODAY - timedelta(days=k), 1599, "streaming")
        s.commit()

        first = detect_subscriptions(s)
        second = detect_subscriptions(s)

        assert first["detected"] == 1
        assert second == {"detected": 0, "updated": 1,
                          "flagged_creep": 0, "flagged_forgotten": 0}
        rows = s.exec(select(Subscription)
                      .where(Subscription.norm_merchant == "netflix")).all()
        assert len(rows) == 1


def test_too_few_charges_skipped():
    with make_session() as s:
        add_txn(s, "newsub", TODAY - timedelta(days=30), 500)
        add_txn(s, "newsub", TODAY, 500)
        s.commit()
        assert detect_subscriptions(s)["detected"] == 0


def test_irregular_cadence_skipped():
    with make_session() as s:
        # Median gap of 15 days falls outside every band.
        add_txn(s, "randomshop", TODAY - timedelta(days=30), 500)
        add_txn(s, "randomshop", TODAY - timedelta(days=15), 500)
        add_txn(s, "randomshop", TODAY, 500)
        s.commit()
        assert detect_subscriptions(s)["detected"] == 0


# Endpoint tests over HTTP against the shared engine.

def _endpoint_client() -> TestClient:
    api = FastAPI()
    api.include_router(router)
    SQLModel.metadata.create_all(shared_engine)
    return TestClient(api)


def _insert_shared_sub(name: str) -> int:
    with Session(shared_engine) as s:
        row = Subscription(name=name, monthly_cents=500, status="review")
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def _cleanup_shared_sub(sub_id: int) -> None:
    with Session(shared_engine) as s:
        row = s.get(Subscription, sub_id)
        if row is not None:
            s.delete(row)
            s.commit()


def test_patch_endpoint_updates_row():
    client = _endpoint_client()
    sub_id = _insert_shared_sub("test-sub-patch")
    try:
        r = client.patch(f"/api/subscriptions/{sub_id}",
                         json={"status": "keep", "moved": True,
                               "monthly": 12.34,
                               "manage_url": "https://example.com/manage"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "keep"
        assert body["moved"] is True
        assert body["monthly"] == 12.34
        assert body["manage_url"] == "https://example.com/manage"
        with Session(shared_engine) as s:
            fresh = s.get(Subscription, sub_id)
            assert fresh.monthly_cents == 1234
            assert fresh.status == "keep"
            assert fresh.moved is True
    finally:
        _cleanup_shared_sub(sub_id)


def test_patch_endpoint_rejects_bad_status():
    client = _endpoint_client()
    sub_id = _insert_shared_sub("test-sub-badstatus")
    try:
        r = client.patch(f"/api/subscriptions/{sub_id}",
                         json={"status": "nonsense"})
        assert r.status_code == 400
        with Session(shared_engine) as s:
            assert s.get(Subscription, sub_id).status == "review"
    finally:
        _cleanup_shared_sub(sub_id)


def test_patch_endpoint_404_on_missing():
    client = _endpoint_client()
    r = client.patch("/api/subscriptions/99999999", json={"status": "keep"})
    assert r.status_code == 404


def test_delete_endpoint_removes_row_then_404s():
    client = _endpoint_client()
    sub_id = _insert_shared_sub("test-sub-delete")
    try:
        r = client.delete(f"/api/subscriptions/{sub_id}")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "deleted": sub_id}
        with Session(shared_engine) as s:
            assert s.get(Subscription, sub_id) is None
        r2 = client.delete(f"/api/subscriptions/{sub_id}")
        assert r2.status_code == 404
    finally:
        _cleanup_shared_sub(sub_id)
