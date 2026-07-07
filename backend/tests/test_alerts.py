"""Alert evaluation: the right things fire, once, and only when new."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.alerts import _big_charge_threshold, evaluate_alerts
from app.canonical import make_txn_uid
from app.models import Alert, Subscription, Transaction
from app.pace import save_cap_cents

TODAY = date(2026, 7, 15)


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


def _kinds(result):
    return {a["kind"] for a in result["alerts"]}


def _dedups(s):
    return set(s.exec(select(Alert.dedup_key)).all())


def test_threshold_floor_and_multiple():
    assert _big_charge_threshold([]) is None
    assert _big_charge_threshold([20_00] * 9) == 150_00        # 3x20 < floor
    assert _big_charge_threshold([100_00] * 9) == 300_00       # 3x100 > floor


def test_first_run_seeds_quietly(db):
    # A charge worth flagging exists before alerts are ever evaluated.
    _txn(db, TODAY - timedelta(days=2), -400_00, "APPLE STORE")
    for i in range(6):
        _txn(db, TODAY - timedelta(days=10 + i), -30_00, f"COFFEE {i}", source_line=i)
    db.commit()
    res = evaluate_alerts(db, today=TODAY, deliver=False)
    assert res["seeded"] is True
    assert res["delivered"] == 0
    # Everything created on the seeding run is pre-marked read (no history dump).
    assert all(a.read for a in db.exec(select(Alert)).all())


def test_big_charge_fires_after_seed_only_when_new(db):
    for i in range(6):
        _txn(db, TODAY - timedelta(days=20 + i), -30_00, f"COFFEE {i}", source_line=i)
    db.commit()
    evaluate_alerts(db, today=TODAY, deliver=False)          # seed
    # A fresh big charge lands after the log exists.
    _txn(db, TODAY - timedelta(days=1), -420_00, "BIG TICKET", source_line=99)
    db.commit()
    res = evaluate_alerts(db, today=TODAY, deliver=False)
    assert res["seeded"] is False
    assert "big_charge" in _kinds(res)
    new = [a for a in res["alerts"] if a["kind"] == "big_charge"]
    assert len(new) == 1 and not new[0]["read"]
    # Old, out-of-window big charges never resurface.
    assert evaluate_alerts(db, today=TODAY, deliver=False)["created"] == 0


def test_old_big_charge_outside_lookback_ignored(db):
    for i in range(6):
        _txn(db, TODAY - timedelta(days=20 + i), -30_00, f"COFFEE {i}", source_line=i)
    _txn(db, TODAY - timedelta(days=50), -900_00, "OLD BIG", source_line=99)  # >35d
    db.commit()
    res = evaluate_alerts(db, today=TODAY, deliver=False)
    assert "big_charge" not in _kinds(res)


def test_pace_over_cap_fires(db):
    save_cap_cents(db, 100_00)                 # tiny cap
    _txn(db, TODAY - timedelta(days=1), -400_00, "RENT PARTIAL")  # already over
    db.commit()
    res = evaluate_alerts(db, today=TODAY, deliver=False)
    assert "pace" in _kinds(res)
    pace = next(a for a in res["alerts"] if a["kind"] == "pace")
    assert pace["severity"] == "warn"


def test_subscription_events(db):
    db.add(Subscription(name="Netflix", monthly_cents=1599, flag="price_creep",
                        last_amount_cents=1599, last_seen_on=TODAY - timedelta(days=3),
                        detected=True, cadence_days=30))
    db.add(Subscription(name="OldGym", monthly_cents=4000, flag="forgotten",
                        last_amount_cents=4000, last_seen_on=TODAY - timedelta(days=80),
                        detected=True, cadence_days=30))
    db.commit()
    res = evaluate_alerts(db, today=TODAY, deliver=False)
    kinds = _kinds(res)
    assert "sub_creep" in kinds
    assert "sub_forgotten" in kinds
    assert "sub_new" in kinds  # Netflix seen recently, never alerted


def test_idempotent(db):
    save_cap_cents(db, 100_00)
    _txn(db, TODAY - timedelta(days=1), -400_00, "SPENDY")
    db.commit()
    first = evaluate_alerts(db, today=TODAY, deliver=False)
    assert first["created"] > 0
    second = evaluate_alerts(db, today=TODAY, deliver=False)
    assert second["created"] == 0
    # No duplicate dedup keys ever land in the log.
    keys = list(db.exec(select(Alert.dedup_key)).all())
    assert len(keys) == len(set(keys))


def test_weekly_rollup_always_present(db):
    res = evaluate_alerts(db, today=TODAY, deliver=False)
    assert "weekly" in _kinds(res)
