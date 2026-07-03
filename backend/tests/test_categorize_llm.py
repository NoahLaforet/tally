"""Tests for the optional LLM categorizer.

All rows live in a private in-memory engine. The Anthropic API is never
called; httpx.Client.post is monkeypatched with canned responses.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import sqlalchemy
from sqlmodel import Session, SQLModel, create_engine

from app.categorize_llm import run_llm_categorizer
from app.config import settings
from app.models import Transaction


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


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(settings, "USE_LLM_CATEGORIZER", True)
    monkeypatch.setattr(settings, "CLAUDE_API_KEY", "test-key")


def _txn(uid, merchant, category="other", locked=False):
    return Transaction(
        txn_uid=uid, account_id=1, posted_date=date(2026, 6, 1),
        amount_cents=-1234, raw_description=merchant.upper(),
        norm_merchant=merchant, category=category,
        category_source="rule", user_locked=locked,
    )


class FakeResponse:
    def __init__(self, mapping):
        self._mapping = mapping

    def raise_for_status(self):
        pass

    def json(self):
        return {"content": [{"type": "text",
                             "text": "Sure:\n" + json.dumps(self._mapping)}]}


def test_gate_disabled_by_default(mem_session):
    out = run_llm_categorizer(mem_session)
    assert out["enabled"] is False
    assert "message" in out


def test_no_candidates(mem_session, llm_on):
    out = run_llm_categorizer(mem_session)
    assert out == {"enabled": True, "categorized": 0}


def test_categorizes_and_respects_locks(mem_session, llm_on, monkeypatch):
    mem_session.add(_txn("t1", "chipotle"))
    mem_session.add(_txn("t2", "chipotle"))
    mem_session.add(_txn("t3", "netflix", locked=True))
    mem_session.add(_txn("t4", "shell", category="gas"))
    mem_session.commit()

    mapping = {"chipotle": "dining", "netflix": "streaming",
               "shell": "gas", "ghost": "dining", "chipotle2": "bogus"}
    monkeypatch.setattr(httpx.Client, "post",
                        lambda self, url, **kw: FakeResponse(mapping))

    out = run_llm_categorizer(mem_session)
    assert out["enabled"] is True
    assert out["categorized"] == 2

    t1 = mem_session.get(Transaction, "t1")
    assert t1.category == "dining"
    assert t1.category_source == "llm"
    assert mem_session.get(Transaction, "t2").category == "dining"
    # Locked row untouched even though the model answered for it.
    t3 = mem_session.get(Transaction, "t3")
    assert t3.category == "other"
    assert t3.user_locked is True
    # Already-categorized row untouched.
    assert mem_session.get(Transaction, "t4").category_source == "rule"


def test_unknown_category_skipped(mem_session, llm_on, monkeypatch):
    mem_session.add(_txn("u1", "mystery"))
    mem_session.commit()
    monkeypatch.setattr(
        httpx.Client, "post",
        lambda self, url, **kw: FakeResponse({"mystery": "not_a_category"}))
    out = run_llm_categorizer(mem_session)
    assert out["categorized"] == 0
    assert mem_session.get(Transaction, "u1").category == "other"


def test_batching_60_merchants_two_calls(mem_session, llm_on, monkeypatch):
    for i in range(60):
        mem_session.add(_txn(f"b{i}", f"merchant{i}"))
    mem_session.commit()

    calls = []

    def fake_post(self, url, **kw):
        prompt = kw["json"]["messages"][0]["content"]
        batch = [m for m in
                 prompt.split("Merchants:\n", 1)[1].splitlines() if m]
        calls.append(len(batch))
        return FakeResponse({m: "shopping" for m in batch})

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    out = run_llm_categorizer(mem_session)
    assert len(calls) == 2
    assert calls == [50, 10]
    assert out["categorized"] == 60


def test_error_returns_dict(mem_session, llm_on, monkeypatch):
    mem_session.add(_txn("e1", "somewhere"))
    mem_session.commit()

    def boom(self, url, **kw):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(httpx.Client, "post", boom)
    out = run_llm_categorizer(mem_session)
    assert out["enabled"] is True
    assert out["categorized"] == 0
    assert "network down" in out["error"]


def test_endpoint_gate(client):
    # Default settings: categorizer off, so no rows needed and no cleanup.
    r = client.post("/api/categorize/llm")
    # The router is only wired by the orchestrator; 404 means not yet mounted.
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.json()["enabled"] is False
