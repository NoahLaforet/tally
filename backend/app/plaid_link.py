"""Plaid live-sync scaffold.

Wires the endpoints for connecting Wells Fargo and Chase via Plaid so transactions
can sync automatically. You (the user) do the actual secure account-linking from
the browser; this code never sees or stores your bank password.

Apple Card is NOT supported by any aggregator, so it always stays on the statement
upload path. This scaffold runs without credentials: every endpoint returns a clear
"not configured" response until PLAID_CLIENT_ID and PLAID_SECRET are set in the env.

When you are ready:
1. Get free Plaid dev credentials at https://dashboard.plaid.com
2. export PLAID_CLIENT_ID=... PLAID_SECRET=... PLAID_ENV=sandbox (or development)
3. Restart Tally; /api/plaid/link-token will return a real token and Plaid Link can run.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import require_user
from .config import settings
from .db import engine
from .events import hub
from .models import PlaidItem
from .secretbox import decrypt_token, encrypt_token, is_encrypted

# Router-level auth: every Plaid endpoint mints tokens for or reads from real
# bank connections, so none of them may be reachable without a session.
router = APIRouter(prefix="/api/plaid", tags=["plaid"],
                   dependencies=[Depends(require_user)])


def _configured() -> bool:
    return bool(settings.PLAID_CLIENT_ID and settings.PLAID_SECRET)


def _client():
    # Imported lazily so the app runs even if plaid is mid-setup.
    import plaid
    from plaid.api import plaid_api

    env = (settings.PLAID_ENV or "sandbox").lower()
    hosts = {"sandbox": plaid.Environment.Sandbox,
             "production": plaid.Environment.Production}
    if env not in hosts:
        # A typo here must not silently fall back to the wrong environment.
        raise HTTPException(500, f"unknown PLAID_ENV '{env}'; use sandbox or production")
    cfg = plaid.Configuration(host=hosts[env], api_key={
        "clientId": settings.PLAID_CLIENT_ID, "secret": settings.PLAID_SECRET})
    return plaid_api.PlaidApi(plaid.ApiClient(cfg))


def _cents(amount) -> int:
    """Plaid amounts arrive as JSON numbers; convert without float arithmetic."""
    return int((Decimal(str(amount)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


@router.get("/status")
def status() -> dict:
    return {"configured": _configured(), "env": settings.PLAID_ENV or "sandbox",
            "note": "Apple Card is statement-upload only; Plaid covers Wells Fargo and Chase."}


@router.post("/link-token")
def link_token() -> dict:
    """Create a Plaid Link token for the browser to open Plaid Link."""
    if not _configured():
        return {"configured": False, "message": "Set PLAID_CLIENT_ID and PLAID_SECRET, then restart."}
    from plaid.model.link_token_create_request import LinkTokenCreateRequest
    from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
    from plaid.model.country_code import CountryCode
    from plaid.model.products import Products

    kwargs = dict(
        user=LinkTokenCreateRequestUser(client_user_id="tally-local-user"),
        client_name="Tally",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    # OAuth banks (Chase, Wells Fargo) need a registered https redirect URI.
    if settings.PLAID_REDIRECT_URI:
        kwargs["redirect_uri"] = settings.PLAID_REDIRECT_URI
    resp = _client().link_token_create(LinkTokenCreateRequest(**kwargs))
    return {"configured": True, "link_token": resp["link_token"]}


class ExchangeBody(BaseModel):
    # POST body, never a query parameter: query strings land in access logs.
    public_token: str


@router.post("/exchange")
def exchange(body: ExchangeBody) -> dict:
    """Exchange the Link public_token for a long-lived access_token.

    The access_token is stored Fernet-encrypted and is never returned or
    logged; /sync decrypts it on use.
    """
    if not _configured():
        return {"configured": False, "message": "Plaid not configured."}
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

    client = _client()
    resp = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=body.public_token))
    item_id, access_token = resp["item_id"], resp["access_token"]
    with Session(engine) as s:
        item = s.get(PlaidItem, item_id)
        if item:
            item.access_token = encrypt_token(access_token)
        else:
            item = PlaidItem(item_id=item_id, access_token=encrypt_token(access_token))
        s.add(item)
        # Map the Item's accounts onto canonical Account rows right away so
        # institution and balances show up before the first transaction sync.
        try:
            _refresh_item_accounts(s, client, item, access_token)
        except Exception:  # noqa: BLE001 - mapping retries on next sync
            pass
        s.commit()
    return {"configured": True, "item_id": item_id, "linked": True}


def _kind_from_subtype(subtype: str) -> str:
    return {"checking": "checking", "savings": "savings",
            "credit card": "credit", "brokerage": "invest"}.get(subtype, "other")


def _map_plaid_account(s: Session, pa, institution: str | None):
    """Attach a Plaid account to its canonical Account row (create if new).

    Statement-ingested accounts and Plaid accounts must be the SAME rows or
    every shared transaction double counts. Known accounts are matched by
    institution + subtype (+ name for the two Wells Fargo cards); anything
    unrecognized gets its own new account with no rewards card mapping.
    """
    from .models import Account

    row = s.exec(select(Account)
                 .where(Account.plaid_account_id == pa.account_id)).first()
    if row is not None:
        return row

    subtype = str(pa.subtype or "").lower()
    name = (pa.name or "").lower()
    inst = (institution or "").lower()

    def by_name(n: str):
        return s.exec(select(Account).where(Account.name == n)).first()

    if "wells" in inst and subtype == "checking":
        row = by_name("Wells Fargo Everyday Checking")
    elif "wells" in inst and subtype == "credit card" and (
            "autograph" in name or "visa" in name):
        row = by_name("Wells Fargo Autograph")
    elif "chase" in inst and subtype == "credit card":
        row = by_name("Chase Freedom Unlimited")
        if row is None:
            row = Account(name="Chase Freedom Unlimited", kind="credit",
                          institution="Chase", is_manual=False, card_key="chase")
            s.add(row)
    if row is None:
        row = Account(name=(pa.name or "Linked account"),
                      kind=_kind_from_subtype(subtype),
                      institution=institution, is_manual=False, card_key=None)
        s.add(row)
    row.plaid_account_id = pa.account_id
    s.add(row)
    s.flush()
    return row


def _refresh_item_accounts(s: Session, client, item: PlaidItem, token: str) -> dict[str, int]:
    """Map every account on the Item and update balances. Returns
    plaid_account_id -> canonical Account.id."""
    from plaid.model.accounts_get_request import AccountsGetRequest

    resp = client.accounts_get(AccountsGetRequest(access_token=token))
    inst = getattr(resp.item, "institution_name", None) or item.institution
    if inst and item.institution != inst:
        item.institution = inst
        s.add(item)
    mapping: dict[str, int] = {}
    for pa in resp.accounts:
        row = _map_plaid_account(s, pa, inst)
        current = getattr(pa.balances, "current", None)
        if current is not None:
            cents = _cents(current)
            # Credit balances arrive as positive "owed"; store as negative net.
            row.balance_cents = -cents if row.kind == "credit" else cents
            s.add(row)
        mapping[pa.account_id] = row.id
    return mapping


@router.post("/sync")
def sync() -> dict:
    """Pull new transactions from every linked Item and converge them with the
    statement-ingested ledger (see ingest/convergence.py): a Plaid transaction
    matching an unlinked statement row links to it instead of inserting, new
    ones insert as origin='plaid', bank-removed ones are cleaned up, and
    pending transactions are skipped until they post. Plaid amounts are
    positive for outflows, so they are negated into our signed-cents
    convention (negative = money out)."""
    if not _configured():
        return {"configured": False, "message": "Plaid not configured."}
    # Never import sandbox's fake transactions into the real database.
    if (settings.PLAID_ENV or "sandbox").lower() == "sandbox":
        return {"configured": True, "synced": 0,
                "message": "Sandbox link verified. Switch to production to import real transactions."}
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    from .ingest.pipeline import _match_transfers
    from .ingest.common import categorize
    from .ingest.convergence import find_statement_match
    from .canonical import make_plaid_uid, normalize_description
    from .models import Transaction

    client = _client()
    added = matched = removed_n = 0
    with Session(engine) as s:
        items = s.exec(select(PlaidItem)).all()
        if not items:
            return {"configured": True, "synced": 0,
                    "message": "No linked accounts yet. Connect a bank first."}
        for item in items:
            token = decrypt_token(item.access_token)
            if not is_encrypted(item.access_token):
                # Legacy plaintext row: encrypt it now that we have the value.
                item.access_token = encrypt_token(token)
                s.add(item)
            acct_map = _refresh_item_accounts(s, client, item, token)
            cursor = item.cursor
            txns, removed = [], []
            while True:
                kw = {"access_token": token}
                if cursor:
                    kw["cursor"] = cursor
                resp = client.transactions_sync(TransactionsSyncRequest(**kw))
                txns.extend(list(resp.added))
                txns.extend(list(resp.modified))
                removed.extend(list(resp.removed))
                cursor = resp.next_cursor
                if not resp.has_more:
                    break

            for t in txns:
                if getattr(t, "pending", False):
                    continue  # imported once it posts
                acct_id = acct_map.get(t.account_id)
                if acct_id is None:
                    acct_id = _map_plaid_account(s, type("PA", (), {
                        "account_id": t.account_id, "name": None,
                        "subtype": None})(), item.institution).id
                amount_cents = -_cents(t.amount)
                merch = (t.merchant_name or t.name or "").strip()
                desc = (t.name or merch).strip()
                existing = s.exec(select(Transaction).where(
                    Transaction.plaid_txn_id == t.transaction_id)).first()
                if existing is not None:
                    # Modified: bank revised details. Statements outrank Plaid,
                    # and user-locked rows are never touched.
                    if existing.origin == "plaid" and not existing.user_locked:
                        existing.posted_date = t.date
                        existing.amount_cents = amount_cents
                        existing.raw_description = desc
                        existing.norm_merchant = normalize_description(merch or desc)
                        s.add(existing)
                    continue
                match = find_statement_match(s, acct_id, t.date, amount_cents)
                if match is not None:
                    match.plaid_txn_id = t.transaction_id
                    s.add(match)
                    matched += 1
                    continue
                uid = make_plaid_uid(t.transaction_id)
                if s.get(Transaction, uid) is not None:
                    continue
                s.add(Transaction(
                    txn_uid=uid, account_id=acct_id, posted_date=t.date,
                    amount_cents=amount_cents, raw_description=desc,
                    norm_merchant=normalize_description(merch or desc),
                    category=categorize(desc, merch, ""),
                    category_source="plaid", origin="plaid",
                    plaid_txn_id=t.transaction_id))
                added += 1

            for r in removed:
                rid = getattr(r, "transaction_id", None) or r
                row = s.exec(select(Transaction)
                             .where(Transaction.plaid_txn_id == rid)).first()
                if row is None:
                    continue
                if row.origin == "plaid":
                    s.delete(row)
                    removed_n += 1
                else:
                    row.plaid_txn_id = None  # statement row stays; just unlink
                    s.add(row)

            item.cursor = cursor
            s.add(item)
        s.commit()
        _match_transfers(s)
    if added or matched or removed_n:
        hub.publish("transactions:updated")
    return {"configured": True, "synced": added, "matched_statements": matched,
            "removed": removed_n}
