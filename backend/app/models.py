"""SQLModel table definitions.

Money rule (hard): every monetary amount is a signed INTEGER number of CENTS.
Never a float. Outflows are negative, inflows are positive. APY is stored in
basis points (bps): 3.40% APY == 340 bps. This avoids all floating point drift
and keeps reconciliation penny exact.

Field naming here is snake_case (Python/SQL side). The REST layer is responsible
for translating to camelCase and to dollar numbers on the way out. See

"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Allowed string enums kept as plain str values for SQLite friendliness.
# Account.kind: checking | savings | invest | credit | debit
# Transaction.category_source: rule | learned | llm | manual | apple | plaid
# Subscription.status: keep | move | review | verify | canceled | discretionary
# Card rules_json holds the reward-rate matrix as JSON text.


class Account(SQLModel, table=True):
    """A bank, card, or investment account."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    kind: str  # checking | savings | invest | credit | debit
    institution: str | None = None
    balance_cents: int = 0
    apy_bps: int = 0  # annual percentage yield in basis points
    plaid_account_id: str | None = Field(default=None, index=True)
    is_manual: bool = True
    # Which rewards card (Card.key) spend on this account earns with. Explicit
    # mapping, set at account creation; None = earns nothing (cash/debit).
    card_key: str | None = None


class Card(SQLModel, table=True):
    """A rewards card and its category rate rules."""

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)  # stable slug, e.g. wf_autograph
    name: str
    rules_json: str = "{}"  # JSON: {category: rate_bps or multiplier}


class Transaction(SQLModel, table=True):
    """A single posted transaction. Amount is signed integer cents."""

    txn_uid: str = Field(primary_key=True)  # deterministic sha256 hex
    account_id: int | None = Field(default=None, foreign_key="account.id", index=True)
    posted_date: date = Field(index=True)
    amount_cents: int  # signed: negative = outflow, positive = inflow
    raw_description: str
    norm_merchant: str = Field(index=True)
    category: str = Field(default="other", index=True)
    category_source: str = "rule"  # rule | learned | llm | manual | apple | plaid
    is_transfer: bool = False
    transfer_group_id: str | None = Field(default=None, index=True)
    source_file_hash: str | None = Field(default=None, index=True)
    source_statement_id: str | None = None
    source_line: int | None = None
    user_locked: bool = False  # if True, auto-categorizer must not overwrite
    first_seen_at: datetime = Field(default_factory=_utcnow)
    # Where the row came from: statement | plaid | ocr. Statements are ground
    # truth; a Plaid row that later appears on a statement is replaced by the
    # statement row, which inherits the link below.
    origin: str = Field(default="statement", index=True)
    # The Plaid transaction this row corresponds to (either inserted from it,
    # or matched to it). Guarantees live-sync and uploads never double count.
    plaid_txn_id: str | None = Field(default=None, index=True)
    # Money that was not really spent by the owner: 'group' = fronted a
    # shared bill and was paid back, 'thirdparty' = someone else's purchase
    # on this card, repaid in full. Both are excluded from all spend math.
    reimbursement: str | None = Field(default=None, index=True)
    # Free-text user tag, e.g. "liam laptop" or "ski trip". The LLM
    # categorizer reads it as an explicit signal when picking a category.
    note: str | None = None


class Category(SQLModel, table=True):
    """A spending category. Builtins ship seeded; customs come from the user
    or from the LLM categorizer proposing one off a transaction note."""

    id: str = Field(primary_key=True)  # stable slug, e.g. ski_trip
    label: str
    color: str = ""  # hex like #fb7185, or empty for the UI default
    hidden: bool = False
    builtin: bool = False


class IngestedFile(SQLModel, table=True):
    """One uploaded source file, deduped by content hash."""

    file_sha256: str = Field(primary_key=True)
    account: str | None = None
    period: str | None = None  # e.g. "2026-03"
    row_count: int = 0
    reconciled: bool = False  # True when parsed totals matched printed totals
    ingested_at: datetime = Field(default_factory=_utcnow)


class Subscription(SQLModel, table=True):
    """A recurring charge plus its card-routing recommendation."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    monthly_cents: int = 0
    category: str = "general"
    current_card: str | None = None
    recommended_card: str | None = None
    status: str = "review"  # keep | move | review | verify | canceled | discretionary
    manage_url: str | None = None
    moved: bool = False  # user marked it as moved to the recommended card
    detected: bool = True  # True if auto detected from transactions
    # Recurrence engine fields. cadence_days is the inferred period (30ish for
    # monthly, 365ish for yearly); flag marks price_creep or forgotten.
    cadence_days: int | None = None
    last_amount_cents: int | None = None
    last_seen_on: date | None = None
    flag: str | None = None  # price_creep | forgotten | None
    norm_merchant: str | None = Field(default=None, index=True)


class BalanceSnapshot(SQLModel, table=True):
    """An account balance observed on a date; the net worth series source.
    One row per (account, day); later observations the same day overwrite."""

    id: int | None = Field(default=None, primary_key=True)
    account_id: int = Field(foreign_key="account.id", index=True)
    taken_on: date = Field(index=True)
    balance_cents: int = 0


class Setting(SQLModel, table=True):
    """Instance-level key/value store (JSON values): savings plan, last sync
    status, per-instance config. Keeps one-off state out of dedicated tables."""

    key: str = Field(primary_key=True)
    value_json: str = "{}"


class Budget(SQLModel, table=True):
    """A monthly spend target for a category."""

    category: str = Field(primary_key=True)
    target_cents: int = 0


class IncomeSource(SQLModel, table=True):
    """A recurring or averaged income stream."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    amount_cents: int = 0  # monthly amount


class LearnedCategory(SQLModel, table=True):
    """User-confirmed merchant to category mapping, used by the categorizer."""

    norm_merchant: str = Field(primary_key=True)
    category: str


class ReimbursementRule(SQLModel, table=True):
    """Merchant-level standing order: every charge from this merchant is
    excluded from spend as 'group' or 'thirdparty'. Created when the user
    marks a charge (the default), applied at every ingest path."""

    norm_merchant: str = Field(primary_key=True)
    kind: str  # group | thirdparty
    created_at: datetime = Field(default_factory=_utcnow)


class User(SQLModel, table=True):
    """Local user record. Foundation for future passkey auth."""

    id: int | None = Field(default=None, primary_key=True)
    handle: str = Field(index=True, unique=True)
    display_name: str
    created_at: datetime = Field(default_factory=_utcnow)


class Credential(SQLModel, table=True):
    """A WebAuthn passkey credential bound to a user.

    Passkeys are origin-bound, so rp_id records which host the credential was
    created on (e.g. "localhost" vs a Tailscale hostname); logins on a host
    only offer the credentials registered there.
    """

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id", index=True)
    credential_id: bytes
    public_key: bytes
    sign_count: int = 0
    rp_id: str = "localhost"
    label: str = ""  # user-facing name, e.g. "MacBook Touch ID"
    created_at: datetime | None = Field(default_factory=_utcnow)


class SetupCode(SQLModel, table=True):
    """A one-time code that authorizes registering a passkey.

    The first code is generated on startup (printed to the server console,
    never stored in plaintext); later ones come from `python -m app.newcode`.
    This is what keeps a fresh instance from being claimed by a stranger who
    can reach the port.
    """

    id: int | None = Field(default=None, primary_key=True)
    code_hash: str = Field(index=True, unique=True)  # sha256 hex of the code
    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    used_at: datetime | None = None


class PlaidItem(SQLModel, table=True):
    """A linked Plaid Item (one bank connection). Holds the access token and the
    transactions/sync cursor. Lives only in the gitignored local DB."""

    item_id: str = Field(primary_key=True)
    access_token: str
    institution: str | None = None
    cursor: str | None = None  # transactions/sync pagination cursor


class Alert(SQLModel, table=True):
    """A fired alert, kept as a log so nothing is push-only.

    dedup_key makes evaluation idempotent: the same condition (a given month's
    pace warning, a specific big charge, a subscription at a specific price)
    only ever creates one row, no matter how often the evaluator runs.
    """

    id: int | None = Field(default=None, primary_key=True)
    # pace | big_charge | sub_creep | sub_forgotten | sub_new | weekly
    kind: str
    dedup_key: str = Field(index=True, unique=True)
    title: str
    body: str
    severity: str = "info"  # info | warn
    created_at: datetime = Field(default_factory=_utcnow)
    read: bool = False
    notified: bool = False  # a macOS notification was delivered for this row
