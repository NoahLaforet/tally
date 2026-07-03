"""Statement/Plaid convergence: matching primitives and shadow replacement.

Every test runs against a private in-memory engine. The functions under test
all accept an explicit session, so nothing here touches the shared test
database. Money is signed integer cents. Outflows are negative.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.ingest.convergence import find_plaid_shadow, find_statement_match
from app.ingest.pipeline import _ensure_account, ingest_file
from app.models import Account, Transaction

POSTED = date(2026, 3, 10)

APPLE_HEADER = (
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)\n"
)


@pytest.fixture
def session():
    # Fresh private engine per test. StaticPool keeps the single in-memory
    # database alive across connections.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _account(session: Session, name: str = "Test Card") -> int:
    row = Account(name=name, kind="credit", is_manual=False)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.id


def _txn(session: Session, uid: str, account_id: int, posted: date,
         cents: int, origin: str, plaid_txn_id: str | None = None,
         **overrides) -> Transaction:
    row = Transaction(
        txn_uid=uid,
        account_id=account_id,
        posted_date=posted,
        amount_cents=cents,
        raw_description=overrides.pop("raw_description", "COFFEE SHOP"),
        norm_merchant=overrides.pop("norm_merchant", "COFFEE SHOP"),
        origin=origin,
        plaid_txn_id=plaid_txn_id,
        **overrides,
    )
    session.add(row)
    session.commit()
    return row


def _write_apple_csv(tmp_path, name: str, rows: list[str]) -> str:
    path = tmp_path / name
    path.write_text(APPLE_HEADER + "".join(r + "\n" for r in rows),
                    encoding="utf-8")
    return str(path)


# ------------------------------------------------------- find_statement_match

def test_find_statement_match_within_window(session):
    acct = _account(session)
    _txn(session, "uid_s1", acct, POSTED, -1234, origin="statement")
    got = find_statement_match(session, acct, POSTED + timedelta(days=2), -1234)
    assert got is not None
    assert got.txn_uid == "uid_s1"


def test_find_statement_match_outside_window(session):
    # MATCH_WINDOW_DAYS is 4, so a 5 day gap is not the same charge.
    acct = _account(session)
    _txn(session, "uid_s1", acct, POSTED, -1234, origin="statement")
    assert find_statement_match(
        session, acct, POSTED + timedelta(days=5), -1234) is None


def test_find_statement_match_skips_already_linked(session):
    # A statement row that already carries a Plaid link is taken; the
    # incoming Plaid transaction must not steal it.
    acct = _account(session)
    _txn(session, "uid_s1", acct, POSTED, -1234, origin="statement",
         plaid_txn_id="ptx_linked")
    assert find_statement_match(session, acct, POSTED, -1234) is None


def test_find_statement_match_wrong_amount(session):
    acct = _account(session)
    _txn(session, "uid_s1", acct, POSTED, -1234, origin="statement")
    assert find_statement_match(session, acct, POSTED, -1233) is None


def test_find_statement_match_wrong_account(session):
    acct = _account(session, "Card A")
    other = _account(session, "Card B")
    _txn(session, "uid_s1", acct, POSTED, -1234, origin="statement")
    assert find_statement_match(session, other, POSTED, -1234) is None


# ---------------------------------------------------------- find_plaid_shadow

def test_find_plaid_shadow_within_window(session):
    acct = _account(session)
    _txn(session, "uid_p1", acct, POSTED, -1234, origin="plaid",
         plaid_txn_id="ptx_1")
    got = find_plaid_shadow(session, acct, POSTED + timedelta(days=2),
                            -1234, set())
    assert got is not None
    assert got.txn_uid == "uid_p1"


def test_find_plaid_shadow_outside_window(session):
    acct = _account(session)
    _txn(session, "uid_p1", acct, POSTED, -1234, origin="plaid",
         plaid_txn_id="ptx_1")
    assert find_plaid_shadow(
        session, acct, POSTED + timedelta(days=5), -1234, set()) is None


def test_find_plaid_shadow_wrong_amount(session):
    acct = _account(session)
    _txn(session, "uid_p1", acct, POSTED, -1234, origin="plaid",
         plaid_txn_id="ptx_1")
    assert find_plaid_shadow(session, acct, POSTED, -1233, set()) is None


def test_find_plaid_shadow_excludes_claimed(session):
    acct = _account(session)
    _txn(session, "uid_p1", acct, POSTED, -1234, origin="plaid",
         plaid_txn_id="ptx_1")
    assert find_plaid_shadow(session, acct, POSTED, -1234, {"uid_p1"}) is None


def test_find_plaid_shadow_claimed_falls_through_to_next(session):
    # Two shadows for the same charge; claiming one yields the other.
    acct = _account(session)
    _txn(session, "uid_p1", acct, POSTED, -1234, origin="plaid",
         plaid_txn_id="ptx_1")
    _txn(session, "uid_p2", acct, POSTED, -1234, origin="plaid",
         plaid_txn_id="ptx_2")
    got = find_plaid_shadow(session, acct, POSTED, -1234, {"uid_p1"})
    assert got is not None
    assert got.txn_uid == "uid_p2"


def test_matchers_respect_origin(session):
    # Each finder only ever returns rows from its own origin.
    acct = _account(session)
    _txn(session, "uid_s1", acct, POSTED, -1234, origin="statement")
    _txn(session, "uid_p1", acct, POSTED, -1234, origin="plaid",
         plaid_txn_id="ptx_1")
    assert find_plaid_shadow(session, acct, POSTED, -1234, set()).txn_uid == "uid_p1"
    assert find_statement_match(session, acct, POSTED, -1234).txn_uid == "uid_s1"


# ---------------------------------------- statement-over-plaid replacement e2e

def test_statement_replaces_plaid_shadow(session, tmp_path):
    account_id = _ensure_account(session, "apple")
    # Plaid inserted this charge first and the user hand set its category.
    # The shadow category must survive the replacement, so pick a CSV row
    # that would categorize as shopping on its own, not dining.
    _txn(session, "uid_ptx_1", account_id, date(2026, 3, 12), -1234,
         origin="plaid", plaid_txn_id="ptx_1",
         category="dining", category_source="manual", user_locked=True)

    path = _write_apple_csv(tmp_path, "apple.csv", [
        "03/10/2026,03/11/2026,GORJANA JEWELRY,Gorjana,Shopping,Purchase,12.34",
    ])
    result = ingest_file(path, session=session)

    assert result["reconciled"] is True
    assert result["inserted"] == 1
    assert result["replacedPlaidRows"] == 1
    # The plaid row is gone.
    assert session.get(Transaction, "uid_ptx_1") is None
    rows = session.exec(
        select(Transaction).where(Transaction.account_id == account_id)).all()
    # The row count did not double.
    assert len(rows) == 1
    row = rows[0]
    assert row.origin == "statement"
    assert row.plaid_txn_id == "ptx_1"
    # The hand set category, its source, and the lock all carried over.
    assert row.category == "dining"
    assert row.category_source == "manual"
    assert row.user_locked is True


def test_statement_inherits_transfer_grouping(session, tmp_path):
    account_id = _ensure_account(session, "apple")
    # The plaid shadow was already matched into a transfer pair. Apple prints
    # payments as negative, Tally flips the sign, so -500.00 lands as +50000.
    _txn(session, "uid_ptx_2", account_id, date(2026, 4, 2), 50000,
         origin="plaid", plaid_txn_id="ptx_2",
         is_transfer=True, transfer_group_id="tg_abc123")

    path = _write_apple_csv(tmp_path, "apple_payment.csv", [
        "04/01/2026,04/02/2026,ACH PAYMENT THANK YOU,Apple,Payment,Payment,-500.00",
    ])
    result = ingest_file(path, session=session)

    assert result["replacedPlaidRows"] == 1
    assert session.get(Transaction, "uid_ptx_2") is None
    rows = session.exec(
        select(Transaction).where(Transaction.account_id == account_id)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.origin == "statement"
    assert row.plaid_txn_id == "ptx_2"
    assert row.is_transfer is True
    assert row.transfer_group_id == "tg_abc123"


def test_duplicate_charges_claim_distinct_shadows(session, tmp_path):
    account_id = _ensure_account(session, "apple")
    # Two identical plaid rows for two identical real charges.
    _txn(session, "uid_ptx_a", account_id, date(2026, 5, 6), -450,
         origin="plaid", plaid_txn_id="ptx_a")
    _txn(session, "uid_ptx_b", account_id, date(2026, 5, 6), -450,
         origin="plaid", plaid_txn_id="ptx_b")

    charge = "05/05/2026,05/06/2026,PRESSED JUICE,Pressed,Restaurants,Purchase,4.50"
    path = _write_apple_csv(tmp_path, "apple_dupes.csv", [charge, charge])
    result = ingest_file(path, session=session)

    assert result["inserted"] == 2
    assert result["replacedPlaidRows"] == 2
    rows = session.exec(
        select(Transaction).where(Transaction.account_id == account_id)).all()
    # Both shadows replaced, count stays 2, no double claiming.
    assert len(rows) == 2
    assert all(r.origin == "statement" for r in rows)
    assert {r.plaid_txn_id for r in rows} == {"ptx_a", "ptx_b"}
