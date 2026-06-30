"""Apple Card CSV parser.

Apple exports a flat transaction list with no running balance, so there is no
opening plus credits minus debits identity to check. Reconciliation here is a
structural gate: every data row must parse into a signed cent amount and a real
date. Apple prints purchases as positive and payments and credits as negative;
Tally stores outflows as negative, so the sign is flipped on the way in.
"""

from __future__ import annotations

import csv
from datetime import date

from ..canonical import CanonicalRecord
from .common import ParseResult, categorize, norm_merchant, period_from_records, to_cents


def is_apple_csv(path: str) -> bool:
    """Detect an Apple Card export by its header row."""
    if not path.lower().endswith(".csv"):
        return False
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = f.readline()
    except OSError:
        return False
    return "Transaction Date" in header and "Amount (USD)" in header


def _parse_date(value: str) -> date:
    """Apple uses MM/DD/YYYY."""
    m, d, y = value.split("/")
    return date(int(y), int(m), int(d))


def parse(path: str, file_hash: str | None = None) -> ParseResult:
    """Parse an Apple Card CSV into canonical records."""
    records: list[CanonicalRecord] = []
    purchases = 0
    purchase_count = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f)):
            raw = (row.get("Description") or "").strip()
            amount_field = (row.get("Amount (USD)") or "").strip()
            if not raw or not amount_field:
                continue
            printed = to_cents(amount_field)  # Apple: + purchase, - payment/credit
            amount_cents = -printed  # Tally: - outflow, + inflow
            merchant = norm_merchant(row.get("Merchant") or raw)
            category = categorize(raw, merchant, row.get("Category", ""))
            is_payment = (row.get("Type") or "").strip().lower() != "purchase"
            if not is_payment:
                purchases += printed
                purchase_count += 1
            records.append(
                CanonicalRecord(
                    account_id="apple",
                    posted_date=_parse_date(row["Transaction Date"]),
                    amount_cents=amount_cents,
                    raw_description=raw,
                    norm_merchant=merchant,
                    category=category,
                    category_source="apple",
                    # is_transfer is left to the pipeline transfer matcher: a card
                    # payment is only a transfer once its opposite leg is found.
                    is_transfer=False,
                    source_file_hash=file_hash,
                    source_statement_id="apple_card_export",
                    source_line=i + 2,  # +1 header, +1 to 1 base
                )
            )
    detail = {
        "rows": len(records),
        "purchase_count": purchase_count,
        "purchases_cents": purchases,
    }
    # Structural reconcile: a non empty export whose every row parsed cleanly.
    reconciled = len(records) > 0
    return ParseResult(
        account="apple",
        records=records,
        reconciled=reconciled,
        detail=detail,
        period=period_from_records(records),
    )
