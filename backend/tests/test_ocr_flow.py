"""OCR ingestion: date header parsing, screenshot text parsing, and the
two-step preview/confirm endpoint flow.

The endpoint test uses the shared app DB, so every row it creates is removed
in a finally block, keyed by the fake upload's content hash.
"""

from __future__ import annotations

import hashlib
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import Session, select

import app.main as main_mod
from app.db import engine
from app.models import Account, IngestedFile, Transaction
from app.ocr_apple import _parse_date_header, parse_apple_card


# ───────────────────────── _parse_date_header ─────────────────────────
def test_date_header_yearless_december_rolls_back_in_january():
    # A December screenshot viewed on Jan 5 must resolve to the past December.
    got = _parse_date_header("December 28", 2026, today=date(2026, 1, 5))
    assert got == date(2025, 12, 28)


def test_date_header_explicit_year_is_kept():
    got = _parse_date_header("June 28, 2026", 2026, today=date(2026, 1, 5))
    assert got == date(2026, 6, 28)


def test_date_header_near_future_keeps_current_year():
    # 5 days ahead is within the 7 day grace window, no rollback.
    got = _parse_date_header("January 10", 2026, today=date(2026, 1, 5))
    assert got == date(2026, 1, 10)
    # Exactly 7 days ahead still keeps the year.
    got = _parse_date_header("January 12", 2026, today=date(2026, 1, 5))
    assert got == date(2026, 1, 12)
    # 8 days ahead rolls back a year.
    got = _parse_date_header("January 13", 2026, today=date(2026, 1, 5))
    assert got == date(2025, 1, 13)


# ───────────────────────── parse_apple_card ─────────────────────────
# Explicit years so the parse is stable no matter what day the test runs.
SYNTHETIC_TEXT = "\n".join([
    "Apple Card",
    "Latest Transactions",
    "June 28, 2026",
    "Chipotle",
    "$12.34",
    "Daily Cash · $0.12",
    "Trader Joe's $45.67",
    "Payment from Chase +$103.47",
    "June 27, 2026",
    "McDonald's",
    "$9.99",
])


def test_parse_apple_card_synthetic_text():
    records = parse_apple_card(SYNTHETIC_TEXT)
    assert len(records) == 4

    chipotle, tjs, payment, mcd = records

    # Merchant line then amount on its own line.
    assert chipotle.raw_description == "Chipotle"
    assert chipotle.amount_cents == -1234
    assert chipotle.posted_date == date(2026, 6, 28)
    assert chipotle.category == "dining"

    # Merchant and amount on the same line. The Daily Cash chrome line in
    # between must not become a row or steal the buffered merchant.
    assert tjs.raw_description == "Trader Joe's"
    assert tjs.amount_cents == -4567
    assert tjs.posted_date == date(2026, 6, 28)
    assert tjs.category == "grocery"

    # Explicit + sign and the word payment both mark an inflow.
    assert payment.raw_description == "Payment from Chase"
    assert payment.amount_cents == 10347
    assert payment.posted_date == date(2026, 6, 28)

    # The second date header moves the running date.
    assert mcd.raw_description == "McDonald's"
    assert mcd.amount_cents == -999
    assert mcd.posted_date == date(2026, 6, 27)

    assert all(r.account_id == "apple" for r in records)
    assert all(r.source_statement_id == "apple_card_screenshot" for r in records)
    assert all(r.category_source == "ocr" for r in records)


# ───────────────────────── endpoint flow ─────────────────────────
def test_ingest_ocr_preview_then_confirm(client: TestClient, monkeypatch):
    # The endpoint saves the upload then OCRs it. Patch the OCR step so the
    # fake png bytes parse to the synthetic screenshot text.
    monkeypatch.setattr(main_mod, "ocr_image", lambda path: SYNTHETIC_TEXT)

    content = b"tally test fake wallet screenshot bytes 4242"
    file_hash = hashlib.sha256(content).hexdigest()

    with Session(engine) as s:
        apple_preexisting = s.exec(
            select(Account).where(Account.name == "Apple Card")).first() is not None

    try:
        r = client.post("/api/ingest",
                        files={"file": ("wallet.png", content, "image/png")})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["needs_confirm"] is True
        assert body["source"] == "ocr"
        token = body["token"]
        assert token

        preview = body["preview"]
        assert len(preview) == 4
        assert body["new"] == 4
        assert [p["merchant"] for p in preview] == [
            "Chipotle", "Trader Joe's", "Payment from Chase", "McDonald's"]
        assert [p["amount"] for p in preview] == [-12.34, -45.67, 103.47, -9.99]
        assert preview[0]["date"] == "2026-06-28"
        assert preview[3]["date"] == "2026-06-27"
        assert all(p["exists"] is False for p in preview)

        # The preview step must not write anything.
        with Session(engine) as s:
            rows = s.exec(select(Transaction)
                          .where(Transaction.source_file_hash == file_hash)).all()
            assert rows == []
            assert s.get(IngestedFile, file_hash) is None

        # Confirm writes the rows.
        r2 = client.post("/api/ingest/confirm", json={"token": token})
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["ok"] is True
        assert body2["added"] == 4

        with Session(engine) as s:
            rows = s.exec(select(Transaction)
                          .where(Transaction.source_file_hash == file_hash)).all()
            assert len(rows) == 4
            assert all(t.origin == "ocr" for t in rows)
            assert sorted(t.amount_cents for t in rows) == [-4567, -1234, -999, 10347]
            assert {t.posted_date for t in rows} == {date(2026, 6, 27), date(2026, 6, 28)}
            f = s.get(IngestedFile, file_hash)
            assert f is not None
            assert f.reconciled is False
            assert f.account == "apple"
            assert f.row_count == 4

        # The token is single use.
        r3 = client.post("/api/ingest/confirm", json={"token": token})
        assert r3.status_code == 410
        assert r3.json()["error"] == "preview_expired"
    finally:
        # Shared DB. Remove every row this test created, keyed by file hash.
        with Session(engine) as s:
            for t in s.exec(select(Transaction)
                            .where(Transaction.source_file_hash == file_hash)).all():
                s.delete(t)
            f = s.get(IngestedFile, file_hash)
            if f is not None:
                s.delete(f)
            s.commit()
            # Confirm may have created the Apple Card account. Delete it only
            # if it did not exist before and nothing else references it now.
            if not apple_preexisting:
                acc = s.exec(select(Account)
                             .where(Account.name == "Apple Card")).first()
                if acc is not None:
                    in_use = s.exec(select(Transaction)
                                    .where(Transaction.account_id == acc.id)).first()
                    if in_use is None:
                        s.delete(acc)
                        s.commit()
