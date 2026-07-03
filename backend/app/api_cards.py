"""Card management: the rewards rules matrix, editable per instance.

Cards drive the rewards-routing math. Rates are basis points per category
(300 = 3%). The seed ships a common starter set; self-hosters edit or replace
them here so their own lineup drives the recommendations.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .auth import require_user
from .db import engine
from .models import Account, Card

router = APIRouter(prefix="/api/cards", tags=["cards"],
                   dependencies=[Depends(require_user)])


def _row(c: Card) -> dict:
    try:
        rules = json.loads(c.rules_json or "{}")
    except json.JSONDecodeError:
        rules = {}
    return {"id": c.id, "key": c.key, "name": c.name, "rules": rules}


class CardBody(BaseModel):
    key: str | None = None
    name: str | None = None
    rules: dict[str, int] | None = None  # category -> bps


@router.get("")
def list_cards() -> list[dict]:
    with Session(engine) as s:
        return [_row(c) for c in s.exec(select(Card)).all()]


@router.post("")
def create_card(body: CardBody) -> dict:
    key = (body.key or "").strip().lower().replace(" ", "_")
    if not key or not body.name:
        raise HTTPException(400, "key and name required")
    with Session(engine) as s:
        if s.exec(select(Card).where(Card.key == key)).first() is not None:
            raise HTTPException(409, "card key already exists")
        c = Card(key=key, name=body.name.strip(),
                 rules_json=json.dumps(body.rules or {}))
        s.add(c)
        s.commit()
        s.refresh(c)
        return _row(c)


@router.patch("/{key}")
def update_card(key: str, body: CardBody) -> dict:
    with Session(engine) as s:
        c = s.exec(select(Card).where(Card.key == key)).first()
        if c is None:
            raise HTTPException(404, "no such card")
        if body.name:
            c.name = body.name.strip()
        if body.rules is not None:
            c.rules_json = json.dumps(body.rules)
        s.add(c)
        s.commit()
        s.refresh(c)
        return _row(c)


@router.delete("/{key}")
def delete_card(key: str) -> dict:
    with Session(engine) as s:
        c = s.exec(select(Card).where(Card.key == key)).first()
        if c is None:
            raise HTTPException(404, "no such card")
        # Accounts pointing at this card fall back to earning nothing.
        cleared = 0
        for a in s.exec(select(Account).where(Account.card_key == key)).all():
            a.card_key = None
            s.add(a)
            cleared += 1
        s.delete(c)
        s.commit()
        return {"ok": True, "deleted": key, "accounts_cleared": cleared}
