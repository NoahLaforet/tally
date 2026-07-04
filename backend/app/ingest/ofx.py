"""OFX / QFX / QBO statement parser.

Both OFX flavors land here. The 1.x files are SGML: leaf tags have no close
tag and a value simply runs until the next angle bracket. The 2.x files are
XML with properly closed tags. Instead of a real SGML parser, the payload is
split on <STMTTRN> and each block is read with a regex whose value capture
stops at the next '<', which is correct for both flavors at once.

Sign convention: OFX amounts already match Tally. Debits arrive negative and
credits arrive positive in TRNAMT, so the printed decimal string is parsed
straight to signed integer cents with no flip and no float anywhere.

Reconcile discipline: an OFX file prints no activity totals, so the gate is
structural like the Apple CSV one. Every STMTTRN block must parse completely,
every FITID must be unique within the file, and at least one row must exist.
A single broken block fails the whole file rather than silently dropping rows.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import date

from fastapi import APIRouter, Depends

from ..auth import require_user
from ..canonical import CanonicalRecord
from .common import ParseResult, categorize, norm_merchant, period_from_records, to_cents

# No routes live here today; uploads reach this parser through the ingest
# pipeline. The router still carries the auth gate so any endpoint added
# later is authenticated by default. It is wired into the app elsewhere.
router = APIRouter(dependencies=[Depends(require_user)])

_EXTENSIONS = (".ofx", ".qfx", ".qbo")


def is_ofx(path: str) -> bool:
    """Detect an OFX family file by extension or by its first 2KB of content."""
    if path.lower().endswith(_EXTENSIONS):
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(2048)
    except OSError:
        return False
    text = head.decode("latin-1", errors="replace").upper()
    return "OFXHEADER" in text or "<OFX>" in text


def _read_text(path: str) -> str:
    """Read the raw payload. OFX 1.x headers often declare CHARSET:1252, so
    fall back to cp1252 when the bytes are not valid UTF-8."""
    with open(path, "rb") as f:
        blob = f.read()
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return blob.decode("cp1252", errors="replace")


def _tag(text: str, name: str) -> str | None:
    """Return the value of the first <NAME> tag in `text`, or None.

    The capture runs from the open tag to the next '<' or end of line. SGML
    values have no close tag and simply end at the next tag or newline; XML
    values end at their close tag. Both stop the same capture.
    """
    m = re.search(rf"<{re.escape(name)}>([^<\r\n]*)", text, re.IGNORECASE)
    if m is None:
        return None
    value = html.unescape(m.group(1)).strip()
    return value or None


def _parse_date(value: str) -> date:
    """DTPOSTED starts with YYYYMMDD; the time and zone suffix are ignored."""
    digits = value[:8]
    if len(digits) != 8 or not digits.isdigit():
        raise ValueError(f"bad OFX date: {value!r}")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def _parse_amount(value: str) -> int:
    """TRNAMT decimal string straight to signed cents, exact string math.

    European exports write comma decimals ('1234,56' or '1.234,56'); normalize
    them to dot-decimal before the cents conversion, because to_cents treats
    commas as thousands separators and would be off by orders of magnitude.
    """
    s = value.strip()
    if s.startswith("+"):
        s = s[1:]
    if not s or not any(c.isdigit() for c in s):
        raise ValueError(f"bad OFX amount: {value!r}")
    if "," in s:
        last_comma, last_dot = s.rfind(","), s.rfind(".")
        if last_comma > last_dot:
            # Comma is the decimal separator; any dots are thousands marks.
            s = s.replace(".", "").replace(",", ".")
    return to_cents(s)


def parse(path: str, file_hash: str | None = None) -> ParseResult:
    """Parse an OFX or QFX file into canonical records."""
    payload = _read_text(path)

    # A credit card statement wraps its list in CCSTMTRS, a bank statement in
    # STMTRS. Check the credit tag; it contains STMTRS as a substring.
    is_credit = re.search(r"<CCSTMTRS>", payload, re.IGNORECASE) is not None
    account_kind = "credit" if is_credit else "checking"

    acctid = _tag(payload, "ACCTID") or ""
    last4 = acctid[-4:] if acctid else "0000"
    org = _tag(payload, "ORG")
    fid = _tag(payload, "FID")
    account_key = "ofx_" + last4
    account_name = "Imported " + (org or "bank") + " x" + last4

    blocks = re.split(r"<STMTTRN>", payload, flags=re.IGNORECASE)[1:]
    records: list[CanonicalRecord] = []
    bad_blocks: list[int] = []
    fitids: list[str] = []
    for i, block in enumerate(blocks, start=1):
        end = re.search(r"</STMTTRN>", block, re.IGNORECASE)
        if end is not None:
            block = block[: end.start()]

        fitid = _tag(block, "FITID")
        name = _tag(block, "NAME")
        memo = _tag(block, "MEMO")
        amount_field = _tag(block, "TRNAMT")
        date_field = _tag(block, "DTPOSTED")
        raw = " ".join(p for p in (name, memo) if p).strip()
        if not fitid or not raw or not amount_field or not date_field:
            bad_blocks.append(i)
            continue
        try:
            amount_cents = _parse_amount(amount_field)  # signs already match Tally
            posted = _parse_date(date_field)
        except ValueError:
            bad_blocks.append(i)
            continue
        fitids.append(fitid)
        merchant = norm_merchant(name or raw)
        records.append(
            CanonicalRecord(
                account_id=account_key,
                posted_date=posted,
                amount_cents=amount_cents,
                raw_description=raw,
                norm_merchant=merchant,
                category=categorize(raw, merchant),
                category_source="rule",
                # is_transfer is left to the pipeline transfer matcher: a card
                # payment is only a transfer once its opposite leg is found.
                is_transfer=False,
                source_file_hash=file_hash,
                # FITID is the bank's own stable id for the row, which keeps
                # dedupe reporting stable across overlapping exports. The
                # txn_uid itself still hashes account, date, amount, merchant,
                # and seq like every other parser.
                source_statement_id=fitid,
                source_line=i,  # STMTTRN block index, 1 based
            )
        )

    counts = Counter(fitids)
    dupe_fitids = sorted(f for f, n in counts.items() if n > 1)

    detail: dict = {
        "rows": len(records),
        "txn_blocks": len(blocks),
        "bad_blocks": bad_blocks,
        "dupe_fitids": dupe_fitids,
        "account_name": account_name,
        "account_kind": account_kind,
        "org": org,
        "fid": fid,
        "acct_last4": last4,
    }

    ledger = re.search(r"<LEDGERBAL>", payload, re.IGNORECASE)
    if ledger is not None:
        bal = _tag(payload[ledger.end():], "BALAMT")
        if bal is not None:
            try:
                # Recorded for the human report only. Without an opening
                # balance there is no opening plus activity equals closing
                # identity, so this number cannot be penny verified and it
                # never gates reconciliation.
                detail["ledger_balance_cents"] = _parse_amount(bal)
            except ValueError:
                pass

    # Structural reconcile: at least one row, every block parsed completely,
    # and no FITID repeated within the file.
    reconciled = bool(records) and not bad_blocks and not dupe_fitids
    return ParseResult(
        account=account_key,
        records=records,
        reconciled=reconciled,
        detail=detail,
        period=period_from_records(records),
    )
