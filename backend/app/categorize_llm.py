"""Optional LLM categorizer for transactions stuck in 'other' or tagged.

Opt-in via USE_LLM_CATEGORIZER plus CLAUDE_API_KEY. Calls the Anthropic
Messages API directly with httpx. Two populations go out per run: distinct
normalized merchant strings still categorized 'other', and every unlocked
transaction carrying a user note. Notes are an explicit user signal, so for
noted rows the merchant, note text, dollar amount, and current category are
sent. The user opted in by enabling the feature; note text is still never
logged or printed anywhere on this side.

The model works against the live category table and may propose new custom
categories from notes; proposals are only created when the same response
actually assigns something to them.
"""

from __future__ import annotations

import json
import re

import httpx
from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from .auth import require_user
from .config import settings
from .db import engine
from .models import Category, Transaction

router = APIRouter(prefix="/api/categorize", tags=["categorize"],
                   dependencies=[Depends(require_user)])

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 50
TIMEOUT_S = 30.0

# New category ids proposed by the model must look like our own slugs.
_NEW_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,23}$")

_RESPONSE_SHAPE = (
    '{"merchants": {merchant: category_id}, '
    '"noted": [{"uid": uid, "category": category_id, '
    '"reimbursement_hint": true|false}], '
    '"new_categories": [{"id": id, "label": label}]}'
)

_RULES = (
    "If a note clearly implies a grouping that fits no existing category, "
    "you may propose a new one: return it in new_categories as {id, label} "
    "with a short lowercase_snake id. Reuse existing categories whenever "
    "reasonable. If a note implies the charge was for someone else or a "
    "shared bill that was paid back, set reimbursement_hint true."
)


def _build_prompt(vocab: dict[str, str], merchants: list[str],
                  noted: list[dict]) -> str:
    parts = [
        "Categorize credit card transactions into these categories "
        "(id: label): "
        + "; ".join(f"{cid}: {label}" for cid, label in vocab.items()) + ".\n"
        "Respond with strict JSON only, shaped as " + _RESPONSE_SHAPE + ". "
        "No other text. Omit any section you have no entries for.\n"
        + _RULES
    ]
    if noted:
        parts.append(
            "Noted transactions, one per line as "
            "uid | merchant | note | amount | current category:\n"
            + "\n".join(
                f"{n['uid']} | {n['merchant']} | {n['note']} | "
                f"{n['amount']} | {n['category']}" for n in noted))
    if merchants:
        # Keep this section last: each merchant on its own line to the end.
        parts.append("Merchants:\n" + "\n".join(merchants))
    return "\n".join(parts)


def _extract_json(text: str) -> dict:
    # Tolerate any surrounding prose by slicing the outermost braces.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object in response")
    obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("response JSON is not an object")
    return obj


def _call_api(prompt: str) -> dict:
    """One Messages API call; returns the parsed JSON object."""
    payload = {
        "model": MODEL,
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": settings.CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    with httpx.Client(timeout=TIMEOUT_S) as client:
        resp = client.post(API_URL, json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    text = "".join(block.get("text", "") for block in body.get("content", []))
    return _extract_json(text)


def _split_response(obj: dict) -> tuple[dict, list, list]:
    """Pull (merchants_map, noted_list, new_categories) out of a response.

    Tolerates missing keys. Also tolerates the old bare shape, a plain
    {merchant: category} object: with no 'merchants' key present, every
    top-level string value is treated as a merchant assignment.
    """
    merchants = obj.get("merchants")
    if not isinstance(merchants, dict):
        if "merchants" not in obj:
            merchants = {k: v for k, v in obj.items() if isinstance(v, str)}
        else:
            merchants = {}
    noted = obj.get("noted")
    if not isinstance(noted, list):
        noted = []
    new_cats = obj.get("new_categories")
    if not isinstance(new_cats, list):
        new_cats = []
    return merchants, noted, new_cats


def _create_referenced_categories(session: Session, new_cats: list,
                                  referenced: set[str],
                                  valid: set[str]) -> list[str]:
    """Create proposed categories that this response actually assigned to.

    Unreferenced proposals are dropped. Ids that collide with an existing
    category or look nothing like a slug are skipped.
    """
    created: list[str] = []
    for item in new_cats:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        label = item.get("label")
        if not isinstance(cid, str) or not _NEW_ID_RE.fullmatch(cid):
            continue
        if cid not in referenced or cid in valid:
            continue
        if session.get(Category, cid) is not None:
            valid.add(cid)
            continue
        if not isinstance(label, str) or not label.strip():
            label = cid.replace("_", " ").title()
        session.add(Category(id=cid, label=label.strip()[:64], builtin=False))
        valid.add(cid)
        created.append(cid)
    return created


def run_llm_categorizer(session: Session, limit: int = 300) -> dict:
    """Categorize 'other' merchants and noted transactions via the Claude API.

    Never raises for missing config or API failures; always returns a dict.
    reimbursement_hint never changes a row's reimbursement field; hinted uids
    are only returned so the UI can surface them in the review queue.
    """
    if not settings.USE_LLM_CATEGORIZER or not settings.CLAUDE_API_KEY:
        return {"enabled": False,
                "message": "set USE_LLM_CATEGORIZER=true and CLAUDE_API_KEY "
                           "to enable the LLM categorizer"}

    # Population (a): distinct merchants stuck in 'other', unlocked.
    merchant_rows = session.exec(
        select(Transaction.norm_merchant)
        .where(Transaction.category == "other")
        .where(Transaction.user_locked == False)  # noqa: E712
        .distinct()
        .limit(limit)
    ).all()
    merchants = [m for m in merchant_rows if m]

    # Population (b): every unlocked transaction with a note, any category.
    noted_txns = session.exec(
        select(Transaction)
        .where(Transaction.note != None)  # noqa: E711
        .where(Transaction.note != "")
        .where(Transaction.user_locked == False)  # noqa: E712
        .limit(limit)
    ).all()

    if not merchants and not noted_txns:
        return {"enabled": True, "categorized": 0}

    # Live vocabulary from the category table; hidden ones stay out of the
    # prompt but remain valid assignment targets.
    vocab = {c.id: c.label for c in session.exec(
        select(Category).where(Category.hidden == False)).all()}  # noqa: E712
    valid = set(session.exec(select(Category.id)).all())

    noted_by_uid = {t.txn_uid: t for t in noted_txns}
    noted_payload = [
        {"uid": t.txn_uid, "merchant": t.norm_merchant,
         "note": (t.note or "").replace("\n", " ").replace("|", "/"),
         "amount": round(t.amount_cents / 100, 2),
         "category": t.category}
        for t in noted_txns
    ]

    # One batch = one API call: merchant batches first, then noted batches.
    batches: list[tuple[list[str], list[dict]]] = []
    for i in range(0, len(merchants), BATCH_SIZE):
        batches.append((merchants[i:i + BATCH_SIZE], []))
    for i in range(0, len(noted_payload), BATCH_SIZE):
        batches.append(([], noted_payload[i:i + BATCH_SIZE]))

    categorized = 0
    created_ids: list[str] = []
    review_hints: list[str] = []
    known_merchants = set(merchants)
    try:
        for batch_merchants, batch_noted in batches:
            obj = _call_api(_build_prompt(vocab, batch_merchants, batch_noted))
            merchants_map, noted_out, new_cats = _split_response(obj)

            referenced = {v for v in merchants_map.values()
                          if isinstance(v, str)}
            referenced |= {n.get("category") for n in noted_out
                           if isinstance(n, dict)
                           and isinstance(n.get("category"), str)}
            fresh = _create_referenced_categories(session, new_cats,
                                                  referenced, valid)
            created_ids += fresh
            for cid in fresh:
                cat = session.get(Category, cid)
                vocab[cid] = cat.label if cat else cid

            for merchant, category in merchants_map.items():
                # Skip anything the model invented or mislabeled.
                if merchant not in known_merchants or category not in valid:
                    continue
                if category == "other":
                    continue
                txns = session.exec(
                    select(Transaction)
                    .where(Transaction.norm_merchant == merchant)
                    .where(Transaction.category == "other")
                    .where(Transaction.user_locked == False)  # noqa: E712
                ).all()
                for t in txns:
                    t.category = category
                    t.category_source = "llm"
                    session.add(t)
                categorized += len(txns)

            for entry in noted_out:
                if not isinstance(entry, dict):
                    continue
                txn = noted_by_uid.get(entry.get("uid"))
                if txn is None or txn.user_locked:
                    continue
                if entry.get("reimbursement_hint") is True \
                        and txn.txn_uid not in review_hints:
                    # Never auto-exclude; the UI surfaces these for review.
                    review_hints.append(txn.txn_uid)
                category = entry.get("category")
                if category not in valid or category == txn.category:
                    continue
                txn.category = category
                txn.category_source = "llm"
                session.add(txn)
                categorized += 1
            session.commit()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError, KeyError) as e:
        session.commit()
        return {"enabled": True, "error": str(e), "categorized": categorized,
                "new_categories": created_ids, "review_hints": review_hints}
    return {"enabled": True, "categorized": categorized,
            "new_categories": created_ids, "review_hints": review_hints}


@router.post("/llm")
def categorize_llm_endpoint() -> dict:
    with Session(engine) as session:
        return run_llm_categorizer(session)
