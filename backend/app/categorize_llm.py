"""Optional LLM categorizer for transactions stuck in 'other'.

Opt-in via USE_LLM_CATEGORIZER plus CLAUDE_API_KEY. Calls the Anthropic
Messages API directly with httpx. Privacy note: ONLY the normalized merchant
strings are sent to the API. No amounts, dates, account names, or any other
transaction data ever leave the machine.
"""

from __future__ import annotations

import json

import httpx
from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from .auth import require_user
from .config import settings
from .db import engine
from .models import Transaction

router = APIRouter(prefix="/api/categorize", tags=["categorize"],
                   dependencies=[Depends(require_user)])

CATEGORIES = [
    "dining", "grocery", "gas", "shopping", "entertainment", "subscriptions",
    "fitness", "transit", "drugstore", "streaming", "apple_services",
    "apple_hardware", "travel", "phone", "other",
]

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 50
TIMEOUT_S = 30.0


def _build_prompt(merchants: list[str]) -> str:
    return (
        "Categorize each merchant into exactly one of these categories: "
        + ", ".join(CATEGORIES) + ".\n"
        "Respond with strict JSON only, an object mapping each merchant "
        "string exactly as given to its category. No other text.\n"
        "Merchants:\n" + "\n".join(merchants)
    )


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


def _call_api(merchants: list[str]) -> dict:
    """One Messages API call for a batch of merchant strings."""
    payload = {
        "model": MODEL,
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": _build_prompt(merchants)}],
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


def run_llm_categorizer(session: Session, limit: int = 300) -> dict:
    """Categorize 'other' transactions by merchant via the Claude API.

    Never raises for missing config or API failures; always returns a dict.
    """
    if not settings.USE_LLM_CATEGORIZER or not settings.CLAUDE_API_KEY:
        return {"enabled": False,
                "message": "set USE_LLM_CATEGORIZER=true and CLAUDE_API_KEY "
                           "to enable the LLM categorizer"}

    rows = session.exec(
        select(Transaction.norm_merchant)
        .where(Transaction.category == "other")
        .where(Transaction.user_locked == False)  # noqa: E712
        .distinct()
        .limit(limit)
    ).all()
    merchants = [m for m in rows if m]
    if not merchants:
        return {"enabled": True, "categorized": 0}

    categorized = 0
    valid = set(CATEGORIES)
    known = set(merchants)
    try:
        for i in range(0, len(merchants), BATCH_SIZE):
            batch = merchants[i:i + BATCH_SIZE]
            mapping = _call_api(batch)
            for merchant, category in mapping.items():
                # Skip anything the model invented or mislabeled.
                if merchant not in known or category not in valid:
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
                if txns:
                    categorized += len(txns)
            session.commit()
    except (httpx.HTTPError, ValueError, json.JSONDecodeError, KeyError) as e:
        session.commit()
        return {"enabled": True, "error": str(e), "categorized": categorized}
    return {"enabled": True, "categorized": categorized}


@router.post("/llm")
def categorize_llm_endpoint() -> dict:
    with Session(engine) as session:
        return run_llm_categorizer(session)
