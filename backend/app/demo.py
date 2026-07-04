"""Clearly-fake demo dataset.

Lets someone evaluate Tally before uploading a statement or connecting a
bank. Loads only into an EMPTY ledger (409 otherwise), everything belongs to
"Demo Bank", and a 'demo_mode' Setting is written so the UI can badge it.
Deterministic: the same data every time, so screenshots and docs stay stable.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, func, select

from .auth import require_user
from .canonical import make_txn_uid, normalize_description
from .db import engine
from .models import (Account, BalanceSnapshot, Card, Setting, Subscription,
                     Transaction)

router = APIRouter(prefix="/api/demo", tags=["demo"],
                   dependencies=[Depends(require_user)])

_MERCHANTS = [
    # (description, category, min dollars, max dollars, per month)
    ("TRADER JOES #112", "grocery", 28, 95, 5),
    ("SAFEWAY STORE 1520", "grocery", 15, 70, 3),
    ("CHIPOTLE 0442", "dining", 11, 18, 4),
    ("LOCAL THAI KITCHEN", "dining", 22, 48, 2),
    ("DOORDASH*BURGERS", "dining", 18, 42, 3),
    ("SHELL OIL 5731", "gas", 30, 62, 3),
    ("AMAZON MKTPL*ORDER", "shopping", 9, 120, 4),
    ("TARGET 00281", "shopping", 12, 80, 2),
    ("AMC THEATRES 0113", "entertainment", 14, 34, 1),
    ("CVS/PHARMACY #921", "drugstore", 6, 25, 1),
    ("CITY METRO TRANSIT", "transit", 2, 6, 6),
]

_SUBS = [
    ("NETFLIX.COM", "streaming", 15.49),
    ("SPOTIFY USA", "streaming", 11.99),
    ("PLANET FITNESS", "fitness", 24.99),
    ("ICLOUD STORAGE", "subscriptions", 2.99),
]


def load_demo(session: Session) -> dict:
    txn_count = session.exec(select(func.count()).select_from(Transaction)).one()
    if txn_count:
        raise HTTPException(409, "demo data loads only into an empty ledger")

    rng = random.Random(42)
    today = date.today()

    checking = Account(name="Demo Checking", kind="checking",
                       institution="Demo Bank", is_manual=True,
                       balance_cents=412_305, card_key=None)
    credit = Account(name="Demo Rewards Card", kind="credit",
                     institution="Demo Bank", is_manual=True,
                     balance_cents=-84_112, card_key="demo_rewards")
    session.add(checking)
    session.add(credit)
    if session.exec(select(Card).where(Card.key == "demo_rewards")).first() is None:
        session.add(Card(key="demo_rewards", name="Demo Rewards Card",
                         rules_json=json.dumps({"dining": 300, "grocery": 200,
                                                "gas": 300, "other": 100})))
    session.flush()

    def add_txn(account: Account, posted: date, dollars: float, desc: str,
                category: str, seq: int = 0, inflow: bool = False) -> None:
        cents = round(dollars * 100)
        amount = cents if inflow else -cents
        uid = make_txn_uid(f"demo:{account.name}", posted, amount, desc, seq)
        session.add(Transaction(
            txn_uid=uid, account_id=account.id, posted_date=posted,
            amount_cents=amount, raw_description=desc,
            norm_merchant=normalize_description(desc), category=category,
            category_source="rule", origin="statement"))

    added = 0
    for months_back in range(6, -1, -1):
        # Real calendar-month arithmetic; 30-day stepping lands two anchors
        # in the same month around February and collides txn uids.
        y, m = today.year, today.month - months_back
        while m < 1:
            y, m = y - 1, m + 12
        month_start = date(y, m, 1)
        # Salary on the 1st, rent on the 3rd, card payment mid-month.
        add_txn(checking, month_start, 3450.00, "DEMO EMPLOYER PAYROLL",
                "other", inflow=True)
        add_txn(checking, month_start + timedelta(days=2), 1650.00,
                "APARTMENT RENT PAYMENT", "other")
        pay = month_start + timedelta(days=14)
        if pay <= today:
            add_txn(checking, pay, 780.00, "CREDIT CARD AUTO PAY", "other")
            add_txn(credit, pay + timedelta(days=1), 780.00,
                    "PAYMENT RECEIVED - THANK YOU", "other", inflow=True)
        for name, cat, price in _SUBS:
            # Stable per-name day of month (python str hash is salted per run).
            d = month_start + timedelta(days=(sum(map(ord, name)) % 24) + 2)
            if d <= today:
                add_txn(credit, d, price, name, cat)
                added += 1
        for desc, cat, lo, hi, per_month in _MERCHANTS:
            for k in range(per_month):
                d = month_start + timedelta(days=rng.randint(0, 27))
                if d > today:
                    continue
                add_txn(credit, d, round(rng.uniform(lo, hi), 2), desc, cat,
                        seq=k)
                added += 1

    for i, acct in enumerate((checking, credit)):
        for weeks_back in range(12, -1, -1):
            session.add(BalanceSnapshot(
                account_id=acct.id,
                taken_on=today - timedelta(weeks=weeks_back),
                balance_cents=acct.balance_cents
                + (weeks_back * (9_000 if i == 0 else -2_500))))

    for name, cat, price in _SUBS:
        session.add(Subscription(
            name=name.title(), monthly_cents=round(price * 100), category=cat,
            status="review", detected=True, cadence_days=30,
            norm_merchant=normalize_description(name)))

    mode = session.get(Setting, "demo_mode") or Setting(key="demo_mode")
    mode.value_json = json.dumps({"enabled": True})
    session.add(mode)
    session.commit()
    return {"ok": True, "transactions": added, "accounts": 2,
            "subscriptions": len(_SUBS)}


@router.post("/load")
def api_load_demo() -> dict:
    with Session(engine) as s:
        return load_demo(s)
