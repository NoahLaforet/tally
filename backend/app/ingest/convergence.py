"""Statement/Plaid convergence.

The same real-world transaction can reach Tally twice: from a statement upload
and from Plaid live-sync. Statements are ground truth (they reconcile to the
penny); Plaid keeps the ledger fresh between statements. Convergence links the
two instead of double counting:

- When a Plaid transaction arrives and an unlinked statement row in the same
  canonical account matches it (same amount, posted within a few days), the
  statement row is linked to the Plaid id and nothing is inserted.
- When a statement arrives covering rows that Plaid already inserted, each
  matching Plaid row is REPLACED by the penny-reconciled statement row, which
  inherits the Plaid link, any transfer grouping, and any category the user
  set by hand.

Matching is amount-exact with a small posted-date window, because the two
sources disagree on posting timestamps but never on amounts.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import Session, select

from ..models import Transaction

MATCH_WINDOW_DAYS = 4


def _candidates(session: Session, account_id: int, posted: date,
                amount_cents: int, origin: str) -> list[Transaction]:
    lo = posted - timedelta(days=MATCH_WINDOW_DAYS)
    hi = posted + timedelta(days=MATCH_WINDOW_DAYS)
    rows = session.exec(
        select(Transaction)
        .where(Transaction.account_id == account_id)
        .where(Transaction.origin == origin)
        .where(Transaction.amount_cents == amount_cents)
        .where(Transaction.posted_date >= lo)
        .where(Transaction.posted_date <= hi)
    ).all()
    rows.sort(key=lambda t: (abs((t.posted_date - posted).days), t.txn_uid))
    return rows


def find_plaid_shadow(session: Session, account_id: int, posted: date,
                      amount_cents: int, claimed: set[str]) -> Transaction | None:
    """A Plaid-inserted row that this statement row supersedes."""
    for t in _candidates(session, account_id, posted, amount_cents, "plaid"):
        if t.txn_uid not in claimed:
            return t
    return None


def find_statement_match(session: Session, account_id: int, posted: date,
                         amount_cents: int) -> Transaction | None:
    """An unlinked statement row that an incoming Plaid transaction matches."""
    for t in _candidates(session, account_id, posted, amount_cents, "statement"):
        if t.plaid_txn_id is None:
            return t
    return None


def learned_category(session: Session, norm_merchant: str) -> str | None:
    """The user-confirmed category for a merchant, if one was ever set.

    Every ingest path (statement, plaid, ocr) consults this at insert time so
    a correction made once in the transactions view sticks for all future
    imports of that merchant.
    """
    from ..models import LearnedCategory

    row = session.get(LearnedCategory, norm_merchant)
    return row.category if row else None
