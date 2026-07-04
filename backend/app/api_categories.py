"""Category management: the builtin set plus user-defined custom categories.

The category table is the live vocabulary for the whole app. Builtins are
seeded rows that can be relabeled, recolored, or hidden but never deleted.
Customs come from the user directly or from the LLM categorizer proposing one
off a transaction note. Deleting a custom category reassigns every transaction
and learned mapping that points at it, so no row is ever orphaned.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, func, select

from .auth import require_user
from .db import get_session
from .models import Budget, Category, LearnedCategory, Transaction

# require_user is defense in depth; the global auth gate middleware also
# covers every /api route when auth is enabled.
router = APIRouter(prefix="/api/categories", tags=["categories"],
                   dependencies=[Depends(require_user)])

# The builtin set. Must stay in sync with the migration 9 seed rows in db.py
# and mirrors main.py's CAT_LABEL fallback dict.
BUILTIN_CATEGORIES = {
    "dining": "Dining & Delivery",
    "grocery": "Groceries",
    "gas": "Gas & EV Charging",
    "apple_hardware": "Apple Hardware (one-time)",
    "apple_services": "Apple Services",
    "shopping": "Shopping",
    "entertainment": "Entertainment",
    "subscriptions": "Subscriptions",
    "fitness": "Fitness",
    "transit": "Transit & Parking",
    "drugstore": "Drugstore",
    "streaming": "Streaming",
    "other": "Other / Misc",
    "transfer": "Account transfers",
}

SLUG_MAX_LEN = 24


def seed_categories(session: Session) -> None:
    """Insert any missing builtin category rows. Safe to call repeatedly.

    Existing rows are left alone so user edits to a builtin's label, color,
    or hidden flag survive restarts.
    """
    changed = False
    for cid, label in BUILTIN_CATEGORIES.items():
        if session.get(Category, cid) is None:
            session.add(Category(id=cid, label=label, builtin=True))
            changed = True
    if changed:
        session.commit()


def slugify(label: str) -> str:
    """Lowercase, non-alphanumerics collapsed to _, trimmed, max 24 chars."""
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    return slug[:SLUG_MAX_LEN].strip("_")


def _row(c: Category, txn_count: int = 0) -> dict:
    return {"id": c.id, "label": c.label, "color": c.color,
            "hidden": c.hidden, "builtin": c.builtin, "txn_count": txn_count}


class CategoryCreate(BaseModel):
    label: str
    color: str | None = None


class CategoryPatch(BaseModel):
    label: str | None = None
    color: str | None = None
    hidden: bool | None = None


@router.get("")
def list_categories(session: Session = Depends(get_session)) -> list[dict]:
    counts = dict(session.exec(
        select(Transaction.category, func.count())
        .group_by(Transaction.category)).all())
    cats = session.exec(
        select(Category).order_by(Category.builtin.desc(), Category.id)).all()
    return [_row(c, counts.get(c.id, 0)) for c in cats]


@router.post("")
def create_category(body: CategoryCreate,
                    session: Session = Depends(get_session)) -> dict:
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "label required")
    cid = slugify(label)
    if not cid:
        raise HTTPException(400, "label must contain letters or digits")
    if session.get(Category, cid) is not None:
        raise HTTPException(409, f"category '{cid}' already exists")
    c = Category(id=cid, label=label, color=(body.color or "").strip(),
                 builtin=False)
    session.add(c)
    session.commit()
    session.refresh(c)
    return _row(c)


@router.patch("/{cat_id}")
def update_category(cat_id: str, body: CategoryPatch,
                    session: Session = Depends(get_session)) -> dict:
    c = session.get(Category, cat_id)
    if c is None:
        raise HTTPException(404, "no such category")
    if body.label is not None:
        label = body.label.strip()
        if not label:
            raise HTTPException(400, "label cannot be empty")
        c.label = label
    if body.color is not None:
        c.color = body.color.strip()
    if body.hidden is not None:
        c.hidden = body.hidden
    session.add(c)
    session.commit()
    session.refresh(c)
    return _row(c)


@router.delete("/{cat_id}")
def delete_category(cat_id: str, reassign_to: str = "other",
                    session: Session = Depends(get_session)) -> dict:
    c = session.get(Category, cat_id)
    if c is None:
        raise HTTPException(404, "no such category")
    if c.builtin:
        raise HTTPException(409, "builtin categories cannot be deleted")
    if reassign_to == cat_id:
        raise HTTPException(400, "reassign_to cannot be the deleted category")
    if session.get(Category, reassign_to) is None:
        raise HTTPException(400, f"reassign_to category '{reassign_to}' "
                                 "does not exist")
    # Locked rows keep their lock but still move; the category is going away.
    txns = session.exec(
        select(Transaction).where(Transaction.category == cat_id)).all()
    for t in txns:
        t.category = reassign_to
        session.add(t)
    learned = session.exec(
        select(LearnedCategory)
        .where(LearnedCategory.category == cat_id)).all()
    for lc in learned:
        lc.category = reassign_to
        session.add(lc)
    budget = session.get(Budget, cat_id)
    if budget is not None:
        session.delete(budget)
    session.delete(c)
    session.commit()
    return {"ok": True, "deleted": cat_id, "reassigned_to": reassign_to,
            "reassigned": len(txns)}
