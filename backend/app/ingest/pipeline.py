"""Ingestion pipeline: detect, parse, reconcile, dedupe, write, match transfers.

The flow for one file:

  1. hash the raw bytes (sha256). If that hash is already in IngestedFile the
     whole file is skipped: ingestion is idempotent at the file level.
  2. detect the format by content and parse to canonical records.
  3. RECONCILE GATE. If the parser could not match printed totals to the penny
     the file is quarantined and nothing is written. A bad statement never
     pollutes the ledger.
  4. resolve the account key to an Account row, assign intra_group_seq to true
     duplicate charges, compute the deterministic txn_uid, and insert with an
     on conflict do nothing semantic so re-ingest never duplicates and never
     overwrites a user locked row.
  5. run the transfer matcher across all still unmatched rows so the two legs
     of a card payment or internal move are linked and flagged. Rows are only
     ever updated, never deleted.
"""

from __future__ import annotations

import hashlib
import shutil
from collections import defaultdict
from datetime import date

from sqlmodel import Session, select

from ..canonical import CanonicalRecord, make_txn_uid
from ..config import settings
from ..db import engine
from ..models import Account, IngestedFile, Transaction
from . import apple_csv, wf_pdf
from .common import ParseResult, looks_like_transfer, pdftext
from .convergence import find_plaid_shadow

# account key -> (display name, kind, institution, rewards card_key)
ACCOUNT_SPECS = {
    "apple": ("Apple Card", "credit", "Goldman Sachs", "apple"),
    "wf_autograph": ("Wells Fargo Autograph", "credit", "Wells Fargo", "wf_autograph"),
    "debit": ("Wells Fargo Everyday Checking", "checking", "Wells Fargo", "debit"),
}


class ReconcileError(Exception):
    """Raised when a statement fails its penny exact reconciliation gate."""

    def __init__(self, path: str, result: ParseResult):
        self.path = path
        self.result = result
        super().__init__(f"reconciliation failed for {path}: {result.detail}")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _detect_and_parse(path: str, file_hash: str) -> ParseResult:
    """Detect the file format by content and return its ParseResult."""
    if apple_csv.is_apple_csv(path):
        return apple_csv.parse(path, file_hash)
    if path.lower().endswith(".pdf"):
        txt = pdftext(path)
        result = wf_pdf.parse(txt, file_hash)
        if result is not None:
            return result
    raise ValueError(f"unrecognized statement format: {path}")


def _quarantine(path: str) -> str:
    """Copy a failing file into data/quarantine and return the destination."""
    qdir = settings.DATA_DIR / "quarantine"
    qdir.mkdir(parents=True, exist_ok=True)
    dest = qdir / __import__("os").path.basename(path)
    shutil.copy2(path, dest)
    return str(dest)


def _ensure_account(session: Session, key: str) -> int:
    """Get or create the Account row for a stable account key, return its id."""
    name, kind, institution, card_key = ACCOUNT_SPECS.get(key, (key, "other", None, None))
    row = session.exec(select(Account).where(Account.name == name)).first()
    if row is None:
        row = Account(name=name, kind=kind, institution=institution,
                      is_manual=False, card_key=card_key)
        session.add(row)
        session.commit()
        session.refresh(row)
    elif row.card_key is None and card_key is not None:
        row.card_key = card_key
        session.add(row)
        session.commit()
    return row.id


def _assign_seq(records: list[CanonicalRecord]) -> None:
    """Disambiguate true duplicate charges within one file via intra_group_seq."""
    seen: dict[tuple, int] = defaultdict(int)
    for r in records:
        groupkey = (r.account_id, r.posted_date, r.amount_cents, r.norm_merchant.upper())
        r.intra_group_seq = seen[groupkey]
        seen[groupkey] += 1


def _transfer_group_id(uid_a: str, uid_b: str) -> str:
    lo, hi = sorted((uid_a, uid_b))
    return "tg_" + hashlib.sha256(f"{lo}|{hi}".encode()).hexdigest()[:16]


def _match_transfers(session: Session) -> int:
    """Pair the two legs of every transfer that is not yet grouped.

    Criteria: opposite sign and equal magnitude, dates within three days,
    different accounts, and at least one leg hitting the transfer lexicon. Both
    legs get is_transfer=True and a shared transfer_group_id. Rows are only
    updated, never deleted. Deterministic group ids make re-running a no-op.
    """
    candidates = session.exec(
        select(Transaction).where(Transaction.transfer_group_id == None)  # noqa: E711
    ).all()
    candidates.sort(key=lambda t: (t.posted_date, t.txn_uid))
    used: set[str] = set()
    matched = 0
    for i, a in enumerate(candidates):
        if a.txn_uid in used or a.amount_cents == 0:
            continue
        for b in candidates[i + 1:]:
            if b.txn_uid in used:
                continue
            if a.account_id == b.account_id:
                continue
            if a.amount_cents != -b.amount_cents:
                continue
            if abs((a.posted_date - b.posted_date).days) > 3:
                continue
            if not (looks_like_transfer(a.raw_description) or looks_like_transfer(b.raw_description)):
                continue
            gid = _transfer_group_id(a.txn_uid, b.txn_uid)
            a.is_transfer = True
            a.transfer_group_id = gid
            b.is_transfer = True
            b.transfer_group_id = gid
            session.add(a)
            session.add(b)
            used.add(a.txn_uid)
            used.add(b.txn_uid)
            matched += 2
            break
    if matched:
        session.commit()
    return matched


def ingest_file(path: str, session: Session | None = None) -> dict:
    """Ingest one statement file end to end. See module docstring for the flow."""
    own_session = session is None
    if own_session:
        session = Session(engine)
    try:
        file_hash = sha256_file(path)

        existing = session.get(IngestedFile, file_hash)
        if existing is not None:
            return {
                "fileSha256": file_hash,
                "account": existing.account,
                "period": existing.period,
                "rowCount": existing.row_count,
                "inserted": 0,
                "duplicate": True,
                "reconciled": existing.reconciled,
                "detail": {},
            }

        result = _detect_and_parse(path, file_hash)

        # RECONCILE GATE: refuse to write a statement that does not balance.
        if not result.reconciled:
            quarantined = _quarantine(path)
            err = ReconcileError(path, result)
            err.quarantined = quarantined
            raise err

        _assign_seq(result.records)
        account_id = _ensure_account(session, result.account)

        inserted = 0
        replaced = 0
        claimed: set[str] = set()  # plaid rows superseded during this ingest
        for r in result.records:
            uid = make_txn_uid(
                r.account_id,  # stable string key, not the autoincrement id
                r.posted_date,
                r.amount_cents,
                r.norm_merchant or r.raw_description,
                r.intra_group_seq,
            )
            if session.get(Transaction, uid) is not None:
                continue  # on conflict do nothing; never overwrite a locked row

            # Statements are ground truth: a Plaid-inserted row covering the
            # same charge is replaced, and the statement row inherits the
            # Plaid link, transfer grouping, and any hand-set category.
            row = Transaction(
                txn_uid=uid,
                account_id=account_id,
                posted_date=r.posted_date,
                amount_cents=r.amount_cents,
                raw_description=r.raw_description,
                norm_merchant=r.norm_merchant,
                category=r.category,
                category_source=r.category_source,
                is_transfer=r.is_transfer,
                transfer_group_id=r.transfer_group_id,
                source_file_hash=file_hash,
                source_statement_id=r.source_statement_id,
                source_line=r.source_line,
                origin="statement",
            )
            shadow = find_plaid_shadow(session, account_id, r.posted_date,
                                       r.amount_cents, claimed)
            if shadow is not None:
                claimed.add(shadow.txn_uid)
                row.plaid_txn_id = shadow.plaid_txn_id
                if shadow.user_locked or shadow.category_source in ("manual", "learned"):
                    row.category = shadow.category
                    row.category_source = shadow.category_source
                    row.user_locked = shadow.user_locked
                if shadow.is_transfer:
                    row.is_transfer = True
                    row.transfer_group_id = shadow.transfer_group_id
                session.delete(shadow)
                replaced += 1
            session.add(row)
            inserted += 1

        session.add(
            IngestedFile(
                file_sha256=file_hash,
                account=result.account,
                period=result.period,
                row_count=len(result.records),
                reconciled=result.reconciled,
            )
        )
        session.commit()

        _match_transfers(session)

        return {
            "fileSha256": file_hash,
            "account": result.account,
            "period": result.period,
            "rowCount": len(result.records),
            "inserted": inserted,
            "replacedPlaidRows": replaced,
            "duplicate": False,
            "reconciled": result.reconciled,
            "detail": result.detail,
        }
    finally:
        if own_session:
            session.close()
