"""Shared ingest helpers: cent parsing, label scraping, merchant and category
normalization, and the parse result contract.

Money rule (hard): every amount is a signed integer number of cents. The cent
parser reads the printed decimal string directly into an integer so there is no
float rounding anywhere between a statement and the database.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from ..canonical import CanonicalRecord

# A money token: 1,234.56 with exactly two decimals. Thousands separators ok.
MONEY = re.compile(r"([\d,]+\.\d{2})")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}
_MONTH_NAME = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})",
    re.IGNORECASE,
)


def pdftext(path: str) -> str:
    """Return the layout preserving text of a PDF via poppler's pdftotext.

    The -layout flag keeps columns aligned, which the Wells Fargo checking
    parser relies on to tell a deposit column from a withdrawal column by
    horizontal position.
    """
    return subprocess.run(
        ["pdftotext", "-layout", path, "-"],
        capture_output=True,
        text=True,
    ).stdout


def to_cents(raw: str) -> int:
    """Parse a printed money string into signed integer cents, no float.

    Handles "$", thousands commas, a leading "-" or a trailing "-" (Wells Fargo
    prints credits as "681.69-"). "12.3" is treated as 12.30.
    """
    s = raw.replace(",", "").replace("$", "").strip()
    neg = False
    if s.endswith("-"):
        neg = True
        s = s[:-1]
    if s.startswith("-"):
        neg = True
        s = s[1:]
    s = s.strip()
    if "." in s:
        whole, frac = s.split(".", 1)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    cents = int(whole or "0") * 100 + int(frac or "0")
    return -cents if neg else cents


def money_after(line: str, label: str) -> int | None:
    """Return the first money token that appears after `label` on `line`.

    Statements are often two column, so the value that belongs to a label is
    the number to its right, not necessarily the first number on the line.
    """
    i = line.find(label)
    if i < 0:
        return None
    m = MONEY.search(line[i + len(label):])
    return to_cents(m.group(1)) if m else None


def grab_money(txt: str, label: str) -> int | None:
    """First occurrence of money_after across all lines of a statement."""
    for ln in txt.splitlines():
        v = money_after(ln, label)
        if v is not None:
            return v
    return None


def statement_close_from_monthname(txt: str) -> tuple[int, int] | None:
    """(year, month) parsed from a header like 'January 27, 2026'."""
    m = _MONTH_NAME.search(txt)
    if not m:
        return None
    return int(m.group(3)), _MONTHS[m.group(1).lower()]


def infer_year(month: int, close_year: int, close_month: int) -> int:
    """Assign a calendar year to a bare MM/DD from a statement close date.

    A statement only looks backward in time, so any transaction month greater
    than the closing month must belong to the previous year (December activity
    on a January statement).
    """
    return close_year if month <= close_month else close_year - 1


def period_from_records(records: list[CanonicalRecord]) -> str | None:
    """The dominant YYYY-MM among a statement's rows, used as IngestedFile.period."""
    if not records:
        return None
    months = Counter(r.posted_date.strftime("%Y-%m") for r in records)
    return months.most_common(1)[0][0]


# ----------------------------------------------------------------- merchant normalize
def norm_merchant(desc: str) -> str:
    """Map a raw description to a short, stable merchant label.

    Ported verbatim from the validated analyze.py rules so the same statement
    yields the same merchant string and therefore the same txn_uid.
    """
    d = desc.upper()
    rules = [
        ("DOORDASH", "DoorDash"), ("UBER *EATS", "Uber Eats"),
        ("UBER   *EATS", "Uber Eats"), ("UBER EATS", "Uber Eats"), ("UBER", "Uber"),
        ("AMAZON PRIME", "Amazon Prime"), ("AMAZON", "Amazon"),
        ("APPLE.COM/BILL", "Apple Services"), ("APPLE SERVICES", "Apple Services"),
        ("APPLE.COM/US", "Apple Store"), ("OPENAI", "ChatGPT"), ("CHATGPT", "ChatGPT"),
        ("NYTIMES", "NYTimes"), ("PEACOCK", "Peacock"), ("HULU", "Hulu"),
        ("ELECTRONIC ARTS", "EA"), ("EA *", "EA"), ("STEAM", "Steam"),
        ("SUPERCELL", "Supercell"), ("SPARKED HOST", "Sparked Host"),
        ("GOOGLE", "Google"), ("LEMONADE", "Lemonade"), ("KALSHI", "Kalshi"),
        ("PRIZEPICKS", "PrizePicks"), ("KRAKEN", "Kraken"), ("COINBASE", "Coinbase"),
        ("FORD MOTOR", "Ford EV"), ("EVGO", "EVgo"), ("CHARGEPOINT", "ChargePoint"),
        ("TESLA SUPERCHARGER", "Tesla"), ("CHEVRON", "Chevron"), ("7-ELEVEN", "7-Eleven"),
        ("TRADER JOE", "Trader Joe's"), ("SAFEWAY", "Safeway"), ("WHOLEFDS", "Whole Foods"),
        ("COSTCO", "Costco"), ("INSTACART", "Instacart"), ("MCDONALD", "McDonald's"),
        ("CHIPOTLE", "Chipotle"), ("PANDA", "Panda Express"), ("IN-N-OUT", "In-N-Out"),
        ("WEST WIND", "West Wind"), ("CVS", "CVS"), ("BLINK", "Blink"), ("CHEGG", "Chegg"),
        ("PHOTO-MATICA", "Photomatica"), ("LIME", "Lime"),
    ]
    for k, v in rules:
        if k in d:
            return v
    return desc.strip().title()[:40]


# ----------------------------------------------------------------- categorize
# Canonical category ids:
#   dining grocery gas shopping entertainment subscriptions fitness transit
#   apple_services apple_hardware drugstore streaming phone travel other
def categorize(desc: str, merchant: str, apple_cat: str = "") -> str:
    """Best effort category id for a transaction. Reward routing only; this is
    not part of the reconciliation gate."""
    d = (merchant + " " + desc).lower()
    ac = (apple_cat or "").lower()
    has = lambda ks: any(k in d for k in ks)

    if merchant == "Apple Store" or "apple.com/us" in d:
        return "apple_hardware"
    if merchant == "Apple Services" or "apple.com/bill" in d:
        return "apple_services"
    if ac == "restaurants":
        return "dining"
    if ac in ("grocery", "alcohol"):
        return "grocery"
    if ac == "gas":
        return "gas"
    if ac == "transportation":
        return "transit"
    if has(["peacock", "hulu", "netflix", "disney", "youtube premium"]):
        return "streaming"
    if has(["chatgpt", "openai", "claude", "ea ", "electronic arts", "chegg",
            "sparked", "nytimes", "amazon prime", "google", "lemonade", "blink", "paddle"]):
        return "subscriptions"
    if has(["cvs", "pharmacy", "walgreens"]):
        return "drugstore"
    if has(["athletic club", "blink fitness", "abc*"]):
        return "fitness"
    if has(["evgo", "chargepoint", "tesla", "chevron", "ford ev", "7-eleven", "gas"]):
        return "gas"
    if has(["doordash", "uber eats", "mcdonald", "chipotle", "panda", "in-n-out",
            "sourdough", "pizza", "poke", "thai", "taqueria", "bagel", "coffee",
            "tst*", "sq *", "dd *", "deli", "burger", "grill", "cookie", "applebee", "chick"]):
        return "dining"
    if has(["trader joe", "safeway", "whole foods", "costco", "instacart", "new leaf", "ralphs"]):
        return "grocery"
    if has(["uber", "lime", "parking", "prking", "parkmobil"]):
        return "transit"
    if has(["west wind", "cinema", "golf", "bowl", "billiard", "steam", "supercell", "amusement"]):
        return "entertainment"
    if has(["amazon", "target", "gorjana", "tiktok", "anthropologie", "rei", "dick", "ace hardware", "nike"]):
        return "shopping"
    return "other"


# Tokens that mark a row as a transfer or card payment leg. Used by the
# pipeline transfer matcher to pair the two legs of an internal move.
TRANSFER_LEXICON = (
    "PAYMENT", "AUTOPAY", "AUTO PAY", "TRANSFER", "ONLINE PAYMENT",
    "WELLS FARGO CARD", "APPLECARD GSBANK", "WF CREDIT CARD",
)


def looks_like_transfer(raw_description: str) -> bool:
    """True if a description hits the transfer lexicon."""
    d = raw_description.upper()
    return any(tok in d for tok in TRANSFER_LEXICON)


@dataclass
class ParseResult:
    """What every statement parser returns.

    `reconciled` is the gate: the pipeline refuses to write a file whose parsed
    totals do not equal the printed totals. `detail` carries the parsed vs
    printed numbers for human readable reporting.
    """

    account: str
    records: list[CanonicalRecord]
    reconciled: bool
    detail: dict = field(default_factory=dict)
    period: str | None = None
