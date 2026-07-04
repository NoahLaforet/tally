"""Plaid account mapping: two accounts must never merge into one row."""

from __future__ import annotations

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Account
from app.plaid_link import _map_plaid_account


class FakePA:
    def __init__(self, account_id, name, subtype, mask=None):
        self.account_id = account_id
        self.name = name
        self.subtype = subtype
        self.mask = mask


def _session():
    engine = create_engine("sqlite://",
                           connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_second_chase_card_gets_its_own_row():
    with _session() as s:
        a = _map_plaid_account(s, FakePA("plaid_a", "Freedom Unlimited",
                                         "credit card", "1111"), "Chase")
        b = _map_plaid_account(s, FakePA("plaid_b", "Sapphire Preferred",
                                         "credit card", "2222"), "Chase")
        s.commit()
        assert a.id != b.id
        assert a.plaid_account_id == "plaid_a"
        assert b.plaid_account_id == "plaid_b"
        assert a.name == "Chase Freedom Unlimited"
        # The second card must not steal or share the canonical row.
        assert "2222" in b.name or b.name != a.name


def test_remap_same_account_is_stable():
    with _session() as s:
        a1 = _map_plaid_account(s, FakePA("plaid_a", "Freedom Unlimited",
                                          "credit card", "1111"), "Chase")
        a2 = _map_plaid_account(s, FakePA("plaid_a", "Freedom Unlimited",
                                          "credit card", "1111"), "Chase")
        assert a1.id == a2.id
        assert len(s.exec(select(Account)).all()) == 1


def test_statement_account_claimed_once_only():
    with _session() as s:
        # Statement ingestion created the canonical checking row earlier.
        s.add(Account(name="Wells Fargo Everyday Checking", kind="checking",
                      institution="Wells Fargo", is_manual=False,
                      card_key="debit"))
        s.commit()
        a = _map_plaid_account(s, FakePA("wf_1", "Everyday Checking",
                                         "checking", "9001"), "Wells Fargo")
        b = _map_plaid_account(s, FakePA("wf_2", "Second Checking",
                                         "checking", "9002"), "Wells Fargo")
        s.commit()
        assert a.name == "Wells Fargo Everyday Checking"
        assert b.id != a.id
        rows = s.exec(select(Account)).all()
        assert len(rows) == 2


def test_same_name_savings_stay_distinct_via_mask():
    with _session() as s:
        a = _map_plaid_account(s, FakePA("sv_1", "WAY2SAVE SAVINGS",
                                         "savings", "5422"), "Wells Fargo")
        b = _map_plaid_account(s, FakePA("sv_2", "WAY2SAVE SAVINGS",
                                         "savings", "6990"), "Wells Fargo")
        assert a.id != b.id
        assert a.name != b.name
