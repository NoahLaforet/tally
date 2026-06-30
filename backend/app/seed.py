"""Seed the Tally database and ingest every real statement.

Run from backend/:  uv run python -m app.seed

It is safe to run repeatedly. Cards and subscriptions are upserted, and the
ingest pipeline is idempotent by file hash, so a second run inserts zero new
transactions and reports the same reconciliation.
"""

from __future__ import annotations

import glob
import json
import os
import re

from sqlmodel import Session, select, func

from .db import engine, init_db
from .ingest.pipeline import ReconcileError, ingest_file
from .models import Card, Subscription, Transaction

# Configurable so no local username/path is baked into the repo. Defaults to the
# app's own data/statements dir; point TALLY_SEED_STATEMENTS at a folder of
# statements (and TALLY_SEED_SUBS at a budget-subs.js) for first-run seeding.
from .config import settings  # noqa: E402

STATEMENTS_DIR = os.environ.get(
    "TALLY_SEED_STATEMENTS", str(os.path.join(settings.DATA_DIR, "statements")))
SUBS_JS = os.environ.get("TALLY_SEED_SUBS", "")

# Reward rate matrix in basis points, sourced from card-strategy.md. 3% == 300.
_CANON_CATS = [
    "dining", "grocery", "gas", "shopping", "entertainment", "subscriptions",
    "fitness", "transit", "apple_services", "apple_hardware", "drugstore",
    "streaming", "phone", "travel", "other",
]


def _rules(overrides: dict[str, int], base: int) -> str:
    rules = {c: base for c in _CANON_CATS}
    rules.update(overrides)
    return json.dumps(rules)


CARD_SEED = [
    ("apple", "Apple Card",
     _rules({"apple_services": 300, "apple_hardware": 200}, 100)),
    ("chase", "Chase Freedom Unlimited",
     _rules({"dining": 300, "drugstore": 300}, 150)),
    ("wf_autograph", "Wells Fargo Autograph",
     _rules({"streaming": 300, "phone": 300, "transit": 300, "gas": 300,
             "dining": 300, "travel": 300}, 100)),
    ("debit", "Wells Fargo Debit (no rewards)",
     _rules({}, 0)),
]

_CARD_CODE = {"apple": "apple", "wf": "wf_autograph", "chase": "chase", "debit": "debit"}


def seed_cards(session: Session) -> int:
    for key, name, rules_json in CARD_SEED:
        row = session.exec(select(Card).where(Card.key == key)).first()
        if row is None:
            session.add(Card(key=key, name=name, rules_json=rules_json))
        else:
            row.name = name
            row.rules_json = rules_json
            session.add(row)
    session.commit()
    return len(CARD_SEED)


def _parse_subs_js(path: str) -> list[dict]:
    text = open(path, encoding="utf-8").read()
    subs = []
    for line in text.splitlines():
        if "name:" not in line or "monthly:" not in line:
            continue

        def grab(field):
            m = re.search(rf'{field}:"([^"]*)"', line)
            return m.group(1) if m else None

        monthly_m = re.search(r"monthly:\s*([\d.]+)", line)
        subs.append({
            "name": grab("name"),
            "monthly": float(monthly_m.group(1)) if monthly_m else 0.0,
            "category": grab("category") or "general",
            "current_card": grab("current_card"),
            "recommended_card": grab("recommended_card"),
            "status": grab("status") or "active",
            "manage_url": grab("manage_url"),
        })
    return subs


def seed_subscriptions(session: Session) -> int:
    rows = _parse_subs_js(SUBS_JS)
    for s in rows:
        current = _CARD_CODE.get(s["current_card"] or "", s["current_card"] or None)
        rec_raw = s["recommended_card"] or ""
        recommended = _CARD_CODE.get(rec_raw, rec_raw) if rec_raw else None
        if s["status"] == "canceled":
            status = "canceled"
        elif recommended and recommended != current:
            status = "move"
        else:
            status = "keep"
        monthly_cents = round(s["monthly"] * 100)

        existing = session.exec(
            select(Subscription).where(Subscription.name == s["name"])
        ).first()
        if existing is None:
            session.add(Subscription(
                name=s["name"], monthly_cents=monthly_cents, category=s["category"],
                current_card=current, recommended_card=recommended, status=status,
                manage_url=s["manage_url"], moved=False, detected=False,
            ))
        else:
            existing.monthly_cents = monthly_cents
            existing.category = s["category"]
            existing.current_card = current
            existing.recommended_card = recommended
            existing.status = status
            existing.manage_url = s["manage_url"]
            existing.detected = False
            session.add(existing)
    session.commit()
    return len(rows)


def _dollars(cents) -> str:
    if cents is None:
        return "n/a"
    return f"${cents / 100:,.2f}"


def _print_reconcile(res: dict) -> None:
    d = res.get("detail", {})
    head = (f"  {os.path.basename('')}".rstrip())
    label = f"{res['account']:14s} period={res.get('period') or '-':9s}"
    status = "DUPLICATE" if res.get("duplicate") else ("OK" if res["reconciled"] else "FAIL")
    print(f"[{status:9s}] {label} rows={res['rowCount']:>4} inserted={res['inserted']:>4}")
    t = d.get("type")
    if t == "wf_autograph":
        print(f"             purchases parsed {_dollars(d['parsed_purchases_cents'])}"
              f" vs printed {_dollars(d['printed_purchases_cents'])}"
              f" | credits parsed {_dollars(d['parsed_credits_cents'])}"
              f" vs printed {_dollars(d['printed_credits_cents'])}"
              f" | new balance {_dollars(d['new_balance_cents'])}")
    elif t == "bilt":
        print(f"             purchases parsed {_dollars(d['parsed_purchases_cents'])}"
              f" vs printed {_dollars(d['printed_purchases_cents'])}"
              f" | payments parsed {_dollars(d['parsed_credits_cents'])}"
              f" vs printed {_dollars(d['printed_payments_cents'])}"
              f" | new balance {_dollars(d['new_balance_cents'])}")
    elif t == "checking":
        print(f"             deposits parsed {_dollars(d['parsed_deposits_cents'])}"
              f" vs printed {_dollars(d['printed_deposits_cents'])}"
              f" | withdrawals parsed {_dollars(d['parsed_withdrawals_cents'])}"
              f" vs printed {_dollars(d['printed_withdrawals_cents'])}")
    elif "purchase_count" in d:
        print(f"             apple rows {d['rows']} purchases {d['purchase_count']}"
              f" totaling {_dollars(d['purchases_cents'])}")


def ingest_all(session: Session) -> tuple[int, int]:
    files = sorted(
        glob.glob(os.path.join(STATEMENTS_DIR, "*.csv"))
        + glob.glob(os.path.join(STATEMENTS_DIR, "*.pdf"))
    )
    inserted_total = 0
    failures = 0
    print(f"\nIngesting {len(files)} statement files from {STATEMENTS_DIR}\n")
    for path in files:
        try:
            res = ingest_file(path, session=session)
            _print_reconcile(res)
            inserted_total += res["inserted"]
        except ReconcileError as e:
            failures += 1
            print(f"[FAIL     ] {os.path.basename(path)} quarantined -> "
                  f"{getattr(e, 'quarantined', '?')}")
            print(f"             detail {e.result.detail}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"[ERROR    ] {os.path.basename(path)}: {e}")
    return inserted_total, failures


def main() -> None:
    init_db()
    with Session(engine) as session:
        before = session.exec(select(func.count()).select_from(Transaction)).one()
        n_cards = seed_cards(session)
        n_subs = seed_subscriptions(session)
        print(f"Seeded {n_cards} cards and {n_subs} subscriptions.")

        inserted, failures = ingest_all(session)

        after = session.exec(select(func.count()).select_from(Transaction)).one()
        transfers = session.exec(
            select(func.count()).select_from(Transaction).where(Transaction.is_transfer == True)  # noqa: E712
        ).one()
        print("\n" + "=" * 72)
        print(f"Transactions in db: {after}  (was {before}, +{after - before} this run)")
        print(f"Inserted this run: {inserted} | reconcile failures: {failures}"
              f" | transfer legs flagged: {transfers}")
        print("=" * 72)


if __name__ == "__main__":
    main()
