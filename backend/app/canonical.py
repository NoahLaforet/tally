"""Canonical record contract shared by every ingest parser.

Each statement parser (Apple CSV, Wells Fargo PDF, Plaid, etc.) must emit a
list of CanonicalRecord. The pipeline then enriches, dedupes, and writes them
as Transaction rows. The deterministic txn_uid is what makes re-ingesting the
same statement idempotent: the same logical row always hashes to the same id.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date

from pydantic import BaseModel, Field

_WS = re.compile(r"\s+")


def normalize_description(description: str) -> str:
    """Collapse whitespace and uppercase for stable, comparable hashing.

    This is intentionally aggressive and lossless of meaning: it only removes
    formatting noise so that "DOORDASH  *DASHPASS" and "DOORDASH *DASHPASS"
    hash to the same canonical token.
    """
    return _WS.sub(" ", description.strip()).upper()


def make_txn_uid(
    account_id: int | str,
    posted_date: date,
    amount_cents: int,
    norm_description: str,
    intra_group_seq: int = 0,
) -> str:
    """Return a deterministic sha256 hex id for a transaction.

    intra_group_seq disambiguates true duplicates within one statement: two
    identical charges (same account, date, amount, merchant) on the same
    statement get seq 0 and 1 so they do not collapse into one row.
    """
    norm = normalize_description(norm_description)
    payload = "|".join(
        [
            str(account_id),
            posted_date.isoformat(),
            str(int(amount_cents)),
            norm,
            str(int(intra_group_seq)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_plaid_uid(transaction_id: str) -> str:
    """Deterministic row id for a Plaid-sourced transaction.

    Keyed on Plaid's own transaction_id, which is unique per transaction, so
    two identical same-day charges stay two rows (no intra_group_seq needed).
    """
    return hashlib.sha256(f"plaid|{transaction_id}".encode("utf-8")).hexdigest()


class CanonicalRecord(BaseModel):
    """A parser's normalized output for one transaction.

    Money is signed integer cents. Fields mirror the enrichment columns on the
    Transaction table so the pipeline can map a record to a row directly.
    """

    account_id: int | str
    posted_date: date
    amount_cents: int  # signed: negative = outflow, positive = inflow
    raw_description: str
    norm_merchant: str
    category: str = "other"
    category_source: str = "rule"
    is_transfer: bool = False
    transfer_group_id: str | None = None
    source_file_hash: str | None = None
    source_statement_id: str | None = None
    source_line: int | None = None
    intra_group_seq: int = Field(default=0)

    def txn_uid(self) -> str:
        """Compute this record's deterministic transaction id."""
        return make_txn_uid(
            self.account_id,
            self.posted_date,
            self.amount_cents,
            self.norm_merchant or self.raw_description,
            self.intra_group_seq,
        )
