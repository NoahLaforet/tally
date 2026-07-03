"""OFX / QFX parser fixture tests.

The fixtures are entirely synthetic: a made up bank, a made up card issuer,
and account numbers that belong to nobody. sample_v1.ofx is the 1.x SGML
flavor with unclosed leaf tags; sample_v2.qfx is the 2.x XML flavor with
closed tags; dupe_fitid.ofx repeats a FITID so the structural gate closes.

Nothing here touches a database. The parser is pure: file in, records out.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.ingest import ofx

FIXTURES = Path(__file__).parent / "fixtures"
V1 = FIXTURES / "sample_v1.ofx"
V2 = FIXTURES / "sample_v2.qfx"
DUPE = FIXTURES / "dupe_fitid.ofx"


# ----------------------------------------------------------------- detection
def test_is_ofx_by_extension(tmp_path):
    assert ofx.is_ofx(str(V1)) is True
    assert ofx.is_ofx(str(V2)) is True
    assert ofx.is_ofx(str(DUPE)) is True

    # .qbo is the QuickBooks spelling of the same format
    qbo = tmp_path / "export.qbo"
    qbo.write_text("anything")
    assert ofx.is_ofx(str(qbo)) is True


def test_is_ofx_by_content(tmp_path):
    # a v1 header in a file with the wrong extension still detects
    sgml = tmp_path / "statement.txt"
    sgml.write_text("OFXHEADER:100\nDATA:OFXSGML\n")
    assert ofx.is_ofx(str(sgml)) is True

    # the v2 processing instruction also contains OFXHEADER
    xml = tmp_path / "statement2.txt"
    xml.write_text('<?xml version="1.0"?>\n<?OFX OFXHEADER="200"?>\n')
    assert ofx.is_ofx(str(xml)) is True

    # a bare <OFX> root is enough
    bare = tmp_path / "statement3.txt"
    bare.write_text("junk before\n<OFX>\n</OFX>\n")
    assert ofx.is_ofx(str(bare)) is True

    # a csv is not an OFX file
    csv = tmp_path / "other.csv"
    csv.write_text("Date,Amount,Memo\n01/01/2026,1.00,x\n")
    assert ofx.is_ofx(str(csv)) is False

    # a missing file without the extension is simply not detected
    assert ofx.is_ofx(str(tmp_path / "nope.txt")) is False


# -------------------------------------------------------------- v1 SGML bank
def test_v1_parses_four_rows_reconciled():
    res = ofx.parse(str(V1), "hash-v1")
    assert res.account == "ofx_5544"
    assert res.reconciled is True
    assert res.period == "2026-04"
    assert len(res.records) == 4

    d = res.detail
    assert d["rows"] == 4
    assert d["txn_blocks"] == 4
    assert d["bad_blocks"] == []
    assert d["dupe_fitids"] == []
    assert d["account_kind"] == "checking"
    assert d["account_name"] == "Imported First Synthetic Bank x5544"
    assert d["acct_last4"] == "5544"
    # recorded for reporting only; not penny verifiable without an opening balance
    assert d["ledger_balance_cents"] == 238134

    # exact cents in file order, debits negative straight from TRNAMT
    assert [r.amount_cents for r in res.records] == [-4567, -1299, 150000, -6000]
    assert [r.posted_date for r in res.records] == [
        date(2026, 4, 2),
        date(2026, 4, 5),
        date(2026, 4, 10),
        date(2026, 4, 21),
    ]

    first = res.records[0]
    # raw description is NAME then MEMO, space joined
    assert first.raw_description == "TRADER JOES 189 SANTA CRUZ POS PURCHASE"
    assert first.norm_merchant == "Trader Joe's"
    assert first.category == "grocery"
    assert first.source_statement_id == "FIT20260402001"
    assert first.source_line == 1
    assert all(r.source_file_hash == "hash-v1" for r in res.records)
    assert all(r.account_id == "ofx_5544" for r in res.records)

    # the OFX credit arrives positive and stays positive, no sign flip
    inflow = res.records[2]
    assert inflow.amount_cents == 150000
    assert inflow.raw_description == "ACME ROBOTICS PAYROLL DIRECT DEPOSIT"

    # a MEMO-less block still gets its raw description from NAME alone
    assert res.records[3].raw_description == "ATM WITHDRAWAL MISSION ST"


# ------------------------------------------------------------ v2 XML credit
def test_v2_qfx_credit_card():
    res = ofx.parse(str(V2), "hash-v2")
    assert res.account == "ofx_1234"
    assert res.reconciled is True
    assert res.period == "2026-05"
    assert len(res.records) == 3

    d = res.detail
    assert d["account_kind"] == "credit"
    assert d["account_name"] == "Imported Synthetic Card Services x1234"
    assert d["ledger_balance_cents"] == -16456
    assert d["bad_blocks"] == []
    assert d["dupe_fitids"] == []

    assert [r.amount_cents for r in res.records] == [-2345, -1199, 20000]

    chipotle = res.records[0]
    assert chipotle.posted_date == date(2026, 5, 3)
    assert chipotle.raw_description == "CHIPOTLE 0042 CAPITOLA CARD PURCHASE"
    assert chipotle.norm_merchant == "Chipotle"
    assert chipotle.category == "dining"
    assert chipotle.source_statement_id == "QFX20260503001"

    # XML close tags never leak into values
    assert all("<" not in r.raw_description for r in res.records)

    # the card payment stays untagged; the pipeline matcher owns is_transfer
    payment = res.records[2]
    assert payment.amount_cents == 20000
    assert payment.is_transfer is False


# ------------------------------------------------------------ reconcile gate
def test_dupe_fitid_fails_reconcile():
    res = ofx.parse(str(DUPE))
    assert res.reconciled is False
    assert res.detail["dupe_fitids"] == ["FITDUPE001"]
    # both rows parse fine on their own; the duplicate id closes the gate
    assert len(res.records) == 2
    assert res.detail["bad_blocks"] == []


def test_tampered_amount_fails_with_block_recorded(tmp_path):
    txt = V1.read_text()
    # 45.67 appears exactly once, on the first transaction line
    assert txt.count("45.67") == 1
    bad = tmp_path / "tampered.ofx"
    bad.write_text(txt.replace("45.67", "45.6x"))

    res = ofx.parse(str(bad))
    assert res.reconciled is False
    assert res.detail["bad_blocks"] == [1]
    # the broken row is dropped from records but fails the whole file
    assert len(res.records) == 3
    assert res.detail["rows"] == 3
    assert res.detail["txn_blocks"] == 4


def test_empty_transaction_list_fails(tmp_path):
    empty = tmp_path / "empty.ofx"
    empty.write_text("OFXHEADER:100\n\n<OFX>\n<STMTRS>\n</STMTRS>\n</OFX>\n")
    res = ofx.parse(str(empty))
    assert res.reconciled is False
    assert res.records == []
    assert res.period is None


# -------------------------------------------------------------------- uids
def test_reparse_yields_identical_uids():
    # the file hash is not part of the uid, so re-exports of the same window
    # hash to the same rows and dedupe holds across overlapping downloads
    a = ofx.parse(str(V1), "hash-one")
    b = ofx.parse(str(V1), "hash-two")
    uids_a = [r.txn_uid() for r in a.records]
    uids_b = [r.txn_uid() for r in b.records]
    assert uids_a == uids_b
    assert len(set(uids_a)) == 4
