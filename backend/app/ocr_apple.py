"""On-device OCR ingestion for Apple Card screenshots.

Apple Card has no statement export aggregator and no CSV from the phone, so the
practical capture path is a screenshot of the transaction list in the Wallet
app. Those images are sensitive, so OCR runs entirely on-device: on macOS we use
the Vision framework (VNRecognizeTextRequest) through a tiny Swift helper that we
compile once and cache; if that is unavailable we fall back to a local Tesseract
install. The pixels never leave the machine.

Two pieces live here:

  * ocr_image(path) -> str: raw recognized text, one line per observation.
  * parse_apple_card(text) -> list[CanonicalRecord]: turn that noisy text into
    canonical transaction rows (account 'apple', purchases negative).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, timedelta

from .canonical import CanonicalRecord
from .ingest.common import categorize, norm_merchant, to_cents

_HERE = os.path.dirname(os.path.abspath(__file__))
_OCR_DIR = os.path.join(os.path.dirname(_HERE), "ocr")  # backend/ocr
_SWIFT_SRC = os.path.join(_OCR_DIR, "vision_ocr.swift")
_SWIFT_BIN = os.path.join(_OCR_DIR, "vision_ocr")


class OCRUnavailable(RuntimeError):
    """Raised when no on-device OCR backend can be used on this platform."""


# ───────────────────────────── OCR backends ─────────────────────────────
def _ensure_vision_binary() -> str | None:
    """Compile the Swift Vision helper once and cache it. Return its path or None.

    Returns None (rather than raising) when we are not on macOS or swiftc is not
    present, so the caller can fall back to another backend.
    """
    if sys.platform != "darwin":
        return None
    if not os.path.exists(_SWIFT_SRC):
        return None
    # Recompile only if the binary is missing or older than the source.
    fresh = (
        os.path.exists(_SWIFT_BIN)
        and os.path.getmtime(_SWIFT_BIN) >= os.path.getmtime(_SWIFT_SRC)
    )
    if fresh:
        return _SWIFT_BIN
    try:
        proc = subprocess.run(
            ["swiftc", "-O", _SWIFT_SRC, "-o", _SWIFT_BIN],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not os.path.exists(_SWIFT_BIN):
        return None
    return _SWIFT_BIN


def _vision_ocr(path: str) -> str | None:
    """Run the cached Vision helper on an image. Return text or None on failure."""
    binary = _ensure_vision_binary()
    if not binary:
        return None
    try:
        proc = subprocess.run(
            [binary, path], capture_output=True, text=True, timeout=120
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _tesseract_ocr(path: str) -> str | None:
    """Fall back to a local pytesseract install if it is importable."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        return pytesseract.image_to_string(Image.open(path))
    except Exception:
        return None


def ocr_image(path: str) -> str:
    """Recognize text in an image on-device.

    Prefers macOS Vision (accurate level), then a local Tesseract. Raises
    OCRUnavailable if neither backend can run on this platform.
    """
    text = _vision_ocr(path)
    if text is None:
        text = _tesseract_ocr(path)
    if text is None:
        raise OCRUnavailable(
            "OCR not available on this platform: needs macOS Vision (swiftc) "
            "or a local pytesseract install."
        )
    return text


# ───────────────────────────── parsing ─────────────────────────────
# A money token as it appears on screen: optional sign, optional $, two decimals.
_MONEY = re.compile(r"([+\-−])?\s*\$?\s*([\d,]+\.\d{2})")

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
# "June 28, 2026" or "June 28"
_DATE_HEADER = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)

# Substrings that mark a line as chrome, not a transaction. Apple shows the
# running balance, the Daily Cash earned per row, payment due banners, and tab
# headers; none of those are spend rows.
_SKIP = (
    "daily cash", "total balance", "card balance", "available credit",
    "no payment", "payment due", "minimum", "interest", "scheduled",
    "available", "balance", "latest transactions", "transactions", "wallet",
    "apple card", "this month", "show more", "search", "credit limit",
    "amount due", "statement",
)


def _parse_date_header(line: str, default_year: int,
                       today: date | None = None) -> date | None:
    """Parse a date from a header line like 'June 28' or 'June 28, 2026'.

    Wallet omits the year for recent transactions. A yearless date that lands
    in the future belongs to the previous year (a December screenshot viewed
    in January must not become next December).
    """
    m = _DATE_HEADER.search(line)
    if not m:
        return None
    month = _MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    explicit_year = m.group(3)
    year = int(explicit_year) if explicit_year else default_year
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if not explicit_year:
        today = today or date.today()
        if (parsed - today).days > 7:
            try:
                parsed = date(year - 1, month, day)
            except ValueError:
                return None
    return parsed


def _is_skip(line: str) -> bool:
    low = line.lower()
    return any(tok in low for tok in _SKIP)


def _is_money_only(line: str) -> bool:
    """True if the line is essentially just a money token (Vision often splits
    the amount into its own observation, separate from the merchant)."""
    stripped = _MONEY.sub("", line).strip(" $\t·•")
    return stripped == "" and _MONEY.search(line) is not None


def parse_apple_card(text: str) -> list[CanonicalRecord]:
    """Parse OCR text from an Apple Card screenshot into canonical records.

    Tolerant of OCR noise: it accepts a merchant and amount on one line, or a
    merchant line followed by an amount on its own line (Vision's usual layout).
    Purchases are stored negative; an explicit '+' or a 'payment'/'refund' line
    is treated as an inflow. Non-transaction lines are skipped.
    """
    today = date.today()
    current_date = today
    pending_merchant: str | None = None
    records: list[CanonicalRecord] = []

    def emit(merchant_text: str, sign_char: str | None, amount_str: str, source_line: int) -> None:
        merchant_text = merchant_text.strip(" -·•\t")
        if not merchant_text or _is_skip(merchant_text):
            return
        magnitude = to_cents(amount_str)
        if magnitude == 0:
            return
        low = merchant_text.lower()
        is_inflow = sign_char in ("+",) or "payment" in low or "refund" in low or "credit" in low
        amount_cents = magnitude if is_inflow else -magnitude
        merchant = norm_merchant(merchant_text)
        category = categorize(merchant_text, merchant)
        # Genuine identical same-day charges are kept: the caller runs
        # _assign_seq to give them distinct intra_group_seq values, and the
        # review-before-import preview is the guard against OCR double reads.
        records.append(
            CanonicalRecord(
                account_id="apple",
                posted_date=current_date,
                amount_cents=amount_cents,
                raw_description=merchant_text,
                norm_merchant=merchant,
                category=category,
                category_source="ocr",
                is_transfer=False,
                source_statement_id="apple_card_screenshot",
                source_line=source_line,
            )
        )

    for i, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip()
        if not line:
            continue

        # A date header updates the running date but is not itself a row.
        hdr = _parse_date_header(line, today.year)
        if hdr is not None and _MONEY.search(line) is None:
            current_date = hdr
            pending_merchant = None
            continue
        low = line.lower()
        if low in _WEEKDAYS or low in ("today", "yesterday"):
            # Wallet uses relative headers for the last week; resolve them so
            # the rows do not all land on the OCR date.
            if low == "today":
                current_date = today
            elif low == "yesterday":
                current_date = today - timedelta(days=1)
            else:
                back = (today.weekday() - _WEEKDAYS.index(low)) % 7 or 7
                current_date = today - timedelta(days=back)
            pending_merchant = None
            continue

        money = _MONEY.search(line)
        if money is None:
            # No amount here: this might be the merchant for a following amount.
            if not _is_skip(line):
                pending_merchant = line
            continue

        if _is_skip(line):
            pending_merchant = None
            continue

        if _is_money_only(line):
            # Amount on its own line: pair it with the buffered merchant.
            if pending_merchant:
                emit(pending_merchant, money.group(1), money.group(2), i + 1)
                pending_merchant = None
            continue

        # Merchant and amount on the same line: text before the amount is the name.
        merchant_text = line[: money.start()].strip()
        emit(merchant_text, money.group(1), money.group(2), i + 1)
        pending_merchant = None

    return records
