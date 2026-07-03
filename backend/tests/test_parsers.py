"""Parser fixture tests: Apple Card CSV, Wells Fargo statement text, pipeline
dedupe, and uid determinism.

The fixtures are synthetic. Their dollar values are chosen so the printed
totals match the transaction lines to the cent, which is exactly what the
reconcile gate checks. The tamper tests then knock one amount off by a cent
and expect the gate to close.

Pipeline tests run on a private in-memory engine so nothing here reads or
writes the shared conftest database.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import sqlalchemy.pool
from sqlmodel import Session, SQLModel, create_engine, select

from app.canonical import make_plaid_uid, make_txn_uid
from app.ingest import apple_csv, wf_pdf
from app.ingest.pipeline import _assign_seq, ingest_file
from app.models import IngestedFile, Transaction

FIXTURES = Path(__file__).parent / "fixtures"
APPLE_GOOD = FIXTURES / "apple_good.csv"
APPLE_BAD = FIXTURES / "apple_bad.csv"
AUTOGRAPH_GOOD = FIXTURES / "wf_autograph_good.txt"
CHECKING_GOOD = FIXTURES / "wf_checking_good.txt"


def private_session() -> Session:
    """A fresh in-memory database per test, never the shared conftest one."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


# ------------------------------------------------------------ Apple Card CSV
def test_is_apple_csv_detects_by_header(tmp_path):
    assert apple_csv.is_apple_csv(str(APPLE_GOOD)) is True
    # the bad fixture still has the Apple header, detection is header only
    assert apple_csv.is_apple_csv(str(APPLE_BAD)) is True

    # a csv with some other header is not an Apple export
    other = tmp_path / "other.csv"
    other.write_text("Date,Amount,Memo\n01/01/2026,1.00,x\n")
    assert apple_csv.is_apple_csv(str(other)) is False

    # the extension gate runs before the header check
    txt = tmp_path / "note.txt"
    txt.write_text("Transaction Date,Amount (USD)\n")
    assert apple_csv.is_apple_csv(str(txt)) is False


def test_apple_good_parses_reconciled():
    res = apple_csv.parse(str(APPLE_GOOD), "hash123")
    assert res.account == "apple"
    assert res.reconciled is True
    assert res.period == "2026-04"
    assert len(res.records) == 8

    d = res.detail
    assert d["rows"] == 8
    assert d["data_rows"] == 8
    assert d["bad_lines"] == []
    assert d["purchase_count"] == 7
    # purchases_cents is the sum of printed purchase magnitudes, positive
    assert d["purchases_cents"] == 12142

    # Apple prints purchases positive, Tally stores outflows negative
    first = res.records[0]
    assert first.posted_date == date(2026, 4, 1)
    assert first.amount_cents == -1549
    assert first.source_line == 2  # header is line 1

    # the payment row (Type != Purchase, printed negative) flips to an inflow
    payment = res.records[2]
    assert payment.amount_cents == 25000
    assert payment.posted_date == date(2026, 4, 3)
    # the transfer flag is left to the pipeline matcher, not set at parse time
    assert payment.is_transfer is False

    purchases = [r for i, r in enumerate(res.records) if i != 2]
    assert all(r.amount_cents < 0 for r in purchases)
    assert all(r.source_file_hash == "hash123" for r in res.records)


def test_apple_bad_amount_fails_reconcile():
    res = apple_csv.parse(str(APPLE_BAD))
    assert res.reconciled is False
    # the garbage amount 12.3x sits on physical line 4 of the file
    assert res.detail["bad_lines"] == [4]
    assert res.detail["data_rows"] == 4
    # the bad row is dropped from records but fails the whole file
    assert len(res.records) == 3


# ------------------------------------------------- Wells Fargo Autograph text
def test_autograph_good_reconciles():
    txt = AUTOGRAPH_GOOD.read_text()
    assert wf_pdf.detect(txt) == "wf_autograph"

    res = wf_pdf.parse(txt, "deadbeef")
    assert res.account == "wf_autograph"
    assert res.reconciled is True
    assert res.period == "2026-04"

    d = res.detail
    assert d["parsed_purchases_cents"] == 8434
    assert d["printed_purchases_cents"] == 8434
    assert d["parsed_credits_cents"] == 25000
    assert d["printed_credits_cents"] == 25000
    assert d["new_balance_cents"] == 33434
    assert d["balance_ok"] is True
    assert d["purchases_ok"] is True
    assert d["credits_ok"] is True

    # 3 purchases as outflows, 1 payment as an inflow
    assert sorted(r.amount_cents for r in res.records) == [-4217, -3000, -1217, 25000]
    # bare MM/DD dates get the statement year
    assert {r.posted_date.year for r in res.records} == {2026}
    payment = next(r for r in res.records if r.amount_cents > 0)
    assert payment.posted_date == date(2026, 4, 10)


def test_autograph_one_cent_off_fails():
    txt = AUTOGRAPH_GOOD.read_text()
    assert txt.count("42.17") == 1  # only the transaction line, not a total
    res = wf_pdf.parse(txt.replace("42.17", "42.18"))
    assert res.reconciled is False
    assert res.detail["purchases_ok"] is False
    assert res.detail["parsed_purchases_cents"] == 8435
    assert res.detail["printed_purchases_cents"] == 8434


def test_autograph_tampered_printed_balance_fails():
    txt = AUTOGRAPH_GOOD.read_text()
    assert txt.count("334.34") == 1
    res = wf_pdf.parse(txt.replace("334.34", "334.35"))
    assert res.reconciled is False
    assert res.detail["balance_ok"] is False


# -------------------------------------------------- Wells Fargo checking text
def test_checking_good_reconciles():
    txt = CHECKING_GOOD.read_text()
    assert wf_pdf.detect(txt) == "checking"

    res = wf_pdf.parse(txt, "cafef00d")
    assert res.account == "debit"
    assert res.reconciled is True
    assert res.period == "2026-04"

    d = res.detail
    assert d["parsed_deposits_cents"] == 50000
    assert d["printed_deposits_cents"] == 50000
    assert d["parsed_withdrawals_cents"] == 30000
    assert d["printed_withdrawals_cents"] == 30000
    assert d["begin_cents"] == 100000
    assert d["end_cents"] == 120000
    assert d["balance_ok"] is True
    assert d["deposits_ok"] is True
    assert d["withdrawals_ok"] is True

    # column position decided deposit vs withdrawal, then the sign convention
    assert sorted(r.amount_cents for r in res.records) == [-20000, -10000, 50000]
    deposit = next(r for r in res.records if r.amount_cents > 0)
    assert deposit.posted_date == date(2026, 4, 10)


def test_checking_one_cent_off_fails():
    txt = CHECKING_GOOD.read_text()
    # 200.00 is a substring of the 1,200.00 ending balance, so tamper the
    # ATM line instead, whose 100.00 appears exactly once
    assert txt.count("100.00") == 1
    res = wf_pdf.parse(txt.replace("100.00", "100.01"))
    assert res.reconciled is False
    assert res.detail["withdrawals_ok"] is False
    assert res.detail["parsed_withdrawals_cents"] == 30001
    assert res.detail["printed_withdrawals_cents"] == 30000


# ------------------------------------------------------------------ pipeline
def test_ingest_idempotent_on_file_hash():
    with private_session() as s:
        r1 = ingest_file(str(APPLE_GOOD), session=s)
        assert r1["duplicate"] is False
        assert r1["reconciled"] is True
        assert r1["inserted"] == 8
        assert r1["rowCount"] == 8
        assert r1["period"] == "2026-04"

        # same bytes, same sha256, whole file skipped
        r2 = ingest_file(str(APPLE_GOOD), session=s)
        assert r2["duplicate"] is True
        assert r2["inserted"] == 0
        assert r2["fileSha256"] == r1["fileSha256"]

        assert len(s.exec(select(Transaction)).all()) == 8
        assert len(s.exec(select(IngestedFile)).all()) == 1


def test_same_day_duplicate_charges_both_survive():
    # seq assignment on the parsed records: 0 then 1 in file order
    res = apple_csv.parse(str(APPLE_GOOD))
    _assign_seq(res.records)
    dups = [r for r in res.records if r.norm_merchant == "Blue Bottle Coffee"]
    assert [r.intra_group_seq for r in dups] == [0, 1]
    assert dups[0].txn_uid() != dups[1].txn_uid()
    # everything else in the file is unique, so it stays at seq 0
    assert all(
        r.intra_group_seq == 0
        for r in res.records
        if r.norm_merchant != "Blue Bottle Coffee"
    )

    # end to end: both duplicate charges land as separate rows
    with private_session() as s:
        ingest_file(str(APPLE_GOOD), session=s)
        rows = s.exec(
            select(Transaction).where(Transaction.norm_merchant == "Blue Bottle Coffee")
        ).all()
        assert len(rows) == 2
        assert rows[0].txn_uid != rows[1].txn_uid
        assert rows[0].posted_date == rows[1].posted_date == date(2026, 4, 7)
        assert rows[0].amount_cents == rows[1].amount_cents == -675


# ------------------------------------------------------------------ uids
def test_make_txn_uid_stable_and_seq_sensitive():
    a = make_txn_uid("apple", date(2026, 4, 7), -675, "Blue Bottle Coffee", 0)
    assert a == make_txn_uid("apple", date(2026, 4, 7), -675, "Blue Bottle Coffee", 0)
    assert len(a) == 64 and set(a) <= set("0123456789abcdef")

    # seq is part of the hash, duplicates never collapse to one uid
    assert make_txn_uid("apple", date(2026, 4, 7), -675, "Blue Bottle Coffee", 1) != a

    # description is whitespace collapsed and uppercased before hashing
    assert make_txn_uid("apple", date(2026, 4, 7), -675, "  blue   BOTTLE coffee ", 0) == a

    # every other field changes the hash
    assert make_txn_uid("debit", date(2026, 4, 7), -675, "Blue Bottle Coffee", 0) != a
    assert make_txn_uid("apple", date(2026, 4, 8), -675, "Blue Bottle Coffee", 0) != a
    assert make_txn_uid("apple", date(2026, 4, 7), -676, "Blue Bottle Coffee", 0) != a

    # lock the payload format so a refactor cannot silently rekey every row
    payload = "apple|2026-04-07|-675|BLUE BOTTLE COFFEE|0"
    assert a == hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_make_plaid_uid():
    a = make_plaid_uid("txn_abc123")
    assert a == make_plaid_uid("txn_abc123")
    assert a != make_plaid_uid("txn_abc124")
    assert len(a) == 64
    # keyed on plaid's own id with a fixed prefix
    assert a == hashlib.sha256(b"plaid|txn_abc123").hexdigest()
