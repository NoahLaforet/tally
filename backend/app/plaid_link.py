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

    resp = _client().item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=body.public_token))
    item_id, access_token = resp["item_id"], resp["access_token"]
    with Session(engine) as s:
        existing = s.get(PlaidItem, item_id)
        if existing:
            existing.access_token = encrypt_token(access_token)
            s.add(existing)
        else:
            s.add(PlaidItem(item_id=item_id, access_token=encrypt_token(access_token)))
        s.commit()
    return {"configured": True, "item_id": item_id, "linked": True}


@router.post("/sync")
def sync() -> dict:
    """Pull new transactions from every linked Item and write them through the
    same txn_uid upsert as statements, so live and uploaded data converge without
    double counting. Plaid amounts are positive for outflows, so they are negated
    into our signed-cents convention (negative = money out)."""
    if not _configured():
        return {"configured": False, "message": "Plaid not configured."}
    # Never import sandbox's fake transactions into the real database.
    if (settings.PLAID_ENV or "sandbox").lower() == "sandbox":
        return {"configured": True, "synced": 0,
                "message": "Sandbox link verified. Switch to production to import real transactions."}
    from plaid.model.transactions_sync_request import TransactionsSyncRequest
    from .ingest.pipeline import _ensure_account, _match_transfers
    from .ingest.common import categorize
    from .canonical import make_txn_uid, normalize_description
    from .models import Transaction

    client = _client()
    total = 0
    with Session(engine) as s:
        items = s.exec(select(PlaidItem)).all()
        if not items:
            return {"configured": True, "synced": 0, "message": "No linked accounts yet. Connect a bank first."}
        for item in items:
            token = decrypt_token(item.access_token)
            if not is_encrypted(item.access_token):
                # Legacy plaintext row: encrypt it now that we have the value.
                item.access_token = encrypt_token(token)
                s.add(item)
            cursor = item.cursor
            txns = []
            while True:
                kw = {"access_token": token}
                if cursor:
                    kw["cursor"] = cursor
                resp = client.transactions_sync(TransactionsSyncRequest(**kw))
                txns.extend(list(resp.added))
                txns.extend(list(resp.modified))
                cursor = resp.next_cursor
                if not resp.has_more:
                    break
            for t in txns:
                acct_key = "plaid:" + t.account_id
                acct_id = _ensure_account(s, acct_key)
                amount_cents = -_cents(t.amount)
                merch = (t.merchant_name or t.name or "").strip()
                desc = (t.name or merch).strip()
                uid = make_txn_uid(acct_key, t.date, amount_cents, merch or desc, 0)
                if s.get(Transaction, uid) is not None:
                    continue
                s.add(Transaction(
                    txn_uid=uid, account_id=acct_id, posted_date=t.date,
                    amount_cents=amount_cents, raw_description=desc,
                    norm_merchant=normalize_description(merch or desc),
                    category=categorize(desc, merch, ""), category_source="plaid"))
                total += 1
            item.cursor = cursor
            s.add(item)
        s.commit()
        _match_transfers(s)
    if total:
        hub.publish("transactions:updated")
    return {"configured": True, "synced": total}
