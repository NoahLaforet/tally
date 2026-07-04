"""Categories API: builtin listing, custom create/patch/delete, reassigns.

Endpoint tests use the shared client fixture, which serves the real app and
the shared test database, so every row created here uses a unique slug or
merchant prefix and is removed in a finally block.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlmodel import Session, select

from app.api_categories import slugify
from app.db import engine
from app.models import Budget, Category, LearnedCategory, Transaction


def _cleanup(cat_ids=(), txn_uids=(), merchants=(), budget_ids=()):
    with Session(engine) as s:
        for uid in txn_uids:
            t = s.get(Transaction, uid)
            if t is not None:
                s.delete(t)
        for m in merchants:
            lc = s.get(LearnedCategory, m)
            if lc is not None:
                s.delete(lc)
        for cid in budget_ids:
            b = s.get(Budget, cid)
            if b is not None:
                s.delete(b)
        for cid in cat_ids:
            c = s.get(Category, cid)
            if c is not None:
                s.delete(c)
        s.commit()


def _txn(uid, merchant, category, locked=False):
    return Transaction(txn_uid=uid, account_id=None,
                       posted_date=date(2026, 6, 1), amount_cents=-2500,
                       raw_description=merchant.upper(),
                       norm_merchant=merchant, category=category,
                       category_source="manual", user_locked=locked)


def test_slugify():
    assert slugify("Liam Laptop") == "liam_laptop"
    assert slugify("  Ski Trip!! 2026  ") == "ski_trip_2026"
    assert slugify("!!!") == ""
    # Trimmed to 24 characters with no dangling underscore.
    assert len(slugify("x" * 40)) == 24
    assert slugify("abcdefghijklmnopqrstuvw m") == "abcdefghijklmnopqrstuvw"


def test_list_builtins_and_txn_counts(client):
    pfx = f"cat{uuid.uuid4().hex[:8]}"
    label = f"{pfx} trip"
    cid = f"{pfx}_trip"
    uids = [f"{pfx}-t1", f"{pfx}-t2"]
    try:
        r = client.post("/api/categories",
                        json={"label": label, "color": "#fb7185"})
        assert r.status_code == 200
        assert r.json() == {"id": cid, "label": label, "color": "#fb7185",
                            "hidden": False, "builtin": False, "txn_count": 0}
        with Session(engine) as s:
            s.add(_txn(uids[0], f"{pfx} shop", cid))
            s.add(_txn(uids[1], f"{pfx} shop", cid))
            s.commit()

        r = client.get("/api/categories")
        assert r.status_code == 200
        by_id = {c["id"]: c for c in r.json()}
        # Every builtin is present and flagged.
        for builtin_id in ("dining", "grocery", "other", "transfer"):
            assert by_id[builtin_id]["builtin"] is True
            assert isinstance(by_id[builtin_id]["txn_count"], int)
        assert by_id[cid]["txn_count"] == 2
        assert by_id[cid]["builtin"] is False
    finally:
        _cleanup(cat_ids=[cid], txn_uids=uids)


def test_create_collision_409_and_bad_labels(client):
    pfx = f"cat{uuid.uuid4().hex[:8]}"
    cid = f"{pfx}_gear"
    try:
        r = client.post("/api/categories", json={"label": f"{pfx} Gear"})
        assert r.status_code == 200
        assert r.json()["id"] == cid
        assert r.json()["color"] == ""
        # A different label that slugs to the same id collides.
        r = client.post("/api/categories", json={"label": f"{pfx} GEAR!!"})
        assert r.status_code == 409
        # Labels that slug to nothing are rejected.
        assert client.post("/api/categories",
                           json={"label": "   "}).status_code == 400
        assert client.post("/api/categories",
                           json={"label": "!!!"}).status_code == 400
    finally:
        _cleanup(cat_ids=[cid])


def test_patch_label_color_hidden(client):
    pfx = f"cat{uuid.uuid4().hex[:8]}"
    cid = f"{pfx}_fund"
    try:
        r = client.post("/api/categories", json={"label": f"{pfx} fund"})
        assert r.status_code == 200
        r = client.patch(f"/api/categories/{cid}",
                         json={"label": "Renamed Fund", "color": "#a3e635",
                               "hidden": True})
        assert r.status_code == 200
        body = r.json()
        assert body["label"] == "Renamed Fund"
        assert body["color"] == "#a3e635"
        assert body["hidden"] is True
        # Hidden categories still show up in the list with the flag set.
        by_id = {c["id"]: c for c in client.get("/api/categories").json()}
        assert by_id[cid]["hidden"] is True

        assert client.patch("/api/categories/no_such_cat_xyz",
                            json={"label": "x"}).status_code == 404
    finally:
        _cleanup(cat_ids=[cid])


def test_patch_builtin_allowed(client):
    # Builtins can be relabeled and recolored; restore afterwards.
    by_id = {c["id"]: c for c in client.get("/api/categories").json()}
    original = by_id["drugstore"]
    try:
        r = client.patch("/api/categories/drugstore",
                         json={"color": "#123456"})
        assert r.status_code == 200
        assert r.json()["color"] == "#123456"
        assert r.json()["builtin"] is True
    finally:
        client.patch("/api/categories/drugstore",
                     json={"color": original["color"]})


def test_delete_reassigns_txns_learned_and_budget(client):
    pfx = f"cat{uuid.uuid4().hex[:8]}"
    cid = f"{pfx}_ski"
    merchant = f"{pfx} resort"
    uids = [f"{pfx}-d1", f"{pfx}-d2"]
    try:
        r = client.post("/api/categories", json={"label": f"{pfx} ski"})
        assert r.status_code == 200
        with Session(engine) as s:
            s.add(_txn(uids[0], merchant, cid))
            # Locked rows still move; the category is being deleted.
            s.add(_txn(uids[1], merchant, cid, locked=True))
            s.add(LearnedCategory(norm_merchant=merchant, category=cid))
            s.add(Budget(category=cid, target_cents=12000))
            s.commit()

        r = client.delete(f"/api/categories/{cid}")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "deleted": cid,
                            "reassigned_to": "other", "reassigned": 2}

        with Session(engine) as s:
            t1 = s.get(Transaction, uids[0])
            t2 = s.get(Transaction, uids[1])
            assert t1.category == "other"
            assert t2.category == "other"
            assert t2.user_locked is True
            assert s.get(LearnedCategory, merchant).category == "other"
            assert s.get(Budget, cid) is None
            assert s.get(Category, cid) is None
    finally:
        _cleanup(cat_ids=[cid], txn_uids=uids, merchants=[merchant],
                 budget_ids=[cid])


def test_delete_builtin_409(client):
    r = client.delete("/api/categories/dining")
    assert r.status_code == 409
    with Session(engine) as s:
        assert s.get(Category, "dining") is not None


def test_delete_guards(client):
    pfx = f"cat{uuid.uuid4().hex[:8]}"
    cid = f"{pfx}_tmp"
    try:
        assert client.post("/api/categories",
                           json={"label": f"{pfx} tmp"}).status_code == 200
        # reassign_to must exist.
        r = client.delete(f"/api/categories/{cid}",
                          params={"reassign_to": "no_such_cat_xyz"})
        assert r.status_code == 400
        # A category cannot absorb itself.
        r = client.delete(f"/api/categories/{cid}",
                          params={"reassign_to": cid})
        assert r.status_code == 400
        assert client.delete("/api/categories/no_such_cat_xyz").status_code == 404
        # Reassign to another custom category works too.
        cid2 = f"{pfx}_kept"
        assert client.post("/api/categories",
                           json={"label": f"{pfx} kept"}).status_code == 200
        r = client.delete(f"/api/categories/{cid}",
                          params={"reassign_to": cid2})
        assert r.status_code == 200
        _cleanup(cat_ids=[cid2])
    finally:
        _cleanup(cat_ids=[cid, f"{pfx}_kept"])


def test_seed_categories_is_idempotent(client):
    from app.api_categories import BUILTIN_CATEGORIES, seed_categories

    with Session(engine) as s:
        before = set(s.exec(select(Category.id)).all())
        seed_categories(s)
        seed_categories(s)
        after = set(s.exec(select(Category.id)).all())
    assert before == after
    assert set(BUILTIN_CATEGORIES) <= after
