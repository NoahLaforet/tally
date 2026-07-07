"""Alerts: the app noticing things so you don't have to watch it.

Four kinds fire:
  - pace        a budget-pace warning when you're projected past your cap
  - big_charge  a single charge well above your usual
  - sub_creep / sub_forgotten / sub_new   subscription events
  - weekly      a once-a-week rollup (also the email digest)

Every alert is written to a log (so nothing is push-only) and, when it is
genuinely new, pushed as a macOS notification. The weekly rollup also goes out
as an email digest when SMTP is configured; it stays silent otherwise.

Evaluation is idempotent by dedup_key, so the daily sync and the midday tick can
both run it without double-firing. The very first evaluation on a database that
already has history SEEDS the log quietly: rows are written but marked read and
nothing is delivered, so turning the feature on doesn't dump months of history
into Notification Center.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from .auth import require_user
from .config import settings
from .db import engine
from .models import Alert, Subscription, Transaction
from .pace import compute_pace
from .spend import instance_lexicons, spend_amount

router = APIRouter(prefix="/api", tags=["alerts"],
                   dependencies=[Depends(require_user)])

BIG_CHARGE_FLOOR_CENTS = 150_00   # never flag anything under this as "big"
BIG_CHARGE_MULTIPLE = 3           # ... or under this many times your median spend
BIG_CHARGE_LOOKBACK_DAYS = 35     # only alert on recent charges, not old imports
BIG_CHARGE_MAX_PER_RUN = 8        # a bulk import shouldn't flood; see the log line
SPEND_WINDOW_DAYS = 90            # trailing window that defines "your usual"


def _money(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def _money2(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _short(text: str, n: int = 42) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _fmt_day(d: date) -> str:
    import calendar
    return f"{calendar.month_abbr[d.month]} {d.day}"


# ─────────────────────────── delivery ───────────────────────────

def notify_macos(title: str, body: str) -> bool:
    """Fire a macOS Notification Center banner. No-op off darwin or when
    ALERTS_NOTIFY is false (the test suite and headless runs stay quiet)."""
    if not settings.ALERTS_NOTIFY or sys.platform != "darwin":
        return False
    script = (f"display notification {json.dumps(body)} "
              f"with title {json.dumps(title)}")
    try:
        subprocess.run(["osascript", "-e", script],
                       check=False, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def send_email(subject: str, body: str) -> bool:
    """Send one email over SMTP if configured; otherwise a silent no-op."""
    if not settings.smtp_configured:
        return False
    import smtplib
    import ssl
    from email.message import EmailMessage
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM or settings.SMTP_USER
        msg["To"] = settings.DIGEST_TO
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as s:
            s.starttls(context=ctx)
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return True
    except Exception:
        return False


# ─────────────────────────── rules ───────────────────────────

def _big_charge_threshold(amounts: list[int]) -> int | None:
    """Cents above which a single charge counts as unusually big for this user:
    the larger of an absolute floor and a multiple of the median spend."""
    if not amounts:
        return None
    ordered = sorted(amounts)
    median = ordered[len(ordered) // 2]
    return max(BIG_CHARGE_FLOOR_CENTS, BIG_CHARGE_MULTIPLE * median)


def _pace_candidates(session: Session, today: date) -> list[dict]:
    p = compute_pace(session, today)
    if not p["cap_set"] or p["state"] != "over":
        return []
    cap_c = round(p["cap"] * 100)
    proj_c = round(p["projected"] * 100)
    return [{
        "kind": "pace",
        "dedup_key": f"pace:{today.year}-{today.month:02d}",
        "title": "Heading over your cap",
        "body": (f"Projected to finish around {_money(proj_c)} this month, "
                 f"past your {_money(cap_c)} cap."),
        "severity": "warn",
    }]


def _big_charge_candidates(session: Session, today: date) -> list[dict]:
    lex = instance_lexicons(session)
    gambling, noncons = lex["gambling"], lex["nonconsumption"]
    window_start = today - timedelta(days=SPEND_WINDOW_DAYS)
    recent_start = today - timedelta(days=BIG_CHARGE_LOOKBACK_DAYS)
    txns = session.exec(
        select(Transaction)
        .where(Transaction.posted_date >= window_start)
        .where(Transaction.posted_date <= today)).all()

    amounts: list[int] = []
    recent: list[tuple[Transaction, int]] = []
    for t in txns:
        amt = spend_amount(t, gambling, noncons)
        if amt is None:
            continue
        amounts.append(amt)
        if t.posted_date >= recent_start:
            recent.append((t, amt))

    threshold = _big_charge_threshold(amounts)
    if threshold is None:
        return []
    # "Unusual" means unexpected. A big charge that matches a known recurring
    # merchant (rent, tuition) is expected, so the subscription system owns it,
    # not the big-charge alert.
    recurring = {s.norm_merchant for s in session.exec(select(Subscription)).all()
                 if s.norm_merchant}
    big = sorted([(t, a) for t, a in recent
                  if a >= threshold and t.norm_merchant not in recurring],
                 key=lambda x: -x[1])
    out = []
    for t, amt in big[:BIG_CHARGE_MAX_PER_RUN]:
        name = _short(t.raw_description or t.norm_merchant)
        out.append({
            "kind": "big_charge",
            "dedup_key": f"big:{t.txn_uid}",
            "title": f"Big charge: {name}",
            "body": (f"{_money(amt)} at {name} on {_fmt_day(t.posted_date)}, "
                     f"above your usual."),
            "severity": "info",
        })
    if len(big) > BIG_CHARGE_MAX_PER_RUN:
        from .events import hub  # local import; hub is trivial
        hub.publish("alerts:capped")  # visible signal that some were dropped
    return out


def _subscription_candidates(session: Session, today: date) -> list[dict]:
    lex = instance_lexicons(session)
    noncons = lex["nonconsumption"]
    out = []
    for s in session.exec(select(Subscription)).all():
        # The recurrence engine also catches card autopay and internal moves;
        # those are not real subscriptions, so don't alert on them.
        hay = f"{s.name} {s.norm_merchant or ''}".lower()
        if any(k in hay for k in noncons):
            continue
        name = s.name
        if s.flag == "price_creep" and s.last_amount_cents:
            out.append({
                "kind": "sub_creep",
                "dedup_key": f"sub_creep:{s.id}:{s.last_amount_cents}",
                "title": f"{name} went up",
                "body": (f"Its latest charge, {_money2(s.last_amount_cents)}, "
                         f"is higher than it used to be. Worth a look."),
                "severity": "warn",
            })
        elif s.flag == "forgotten" and s.last_seen_on:
            out.append({
                "kind": "sub_forgotten",
                "dedup_key": f"sub_forgotten:{s.id}:{s.last_seen_on.isoformat()}",
                "title": f"Still paying for {name}?",
                "body": (f"No charge since {_fmt_day(s.last_seen_on)}, but it "
                         f"usually renews. Cancel it if you're done."),
                "severity": "info",
            })
        # Newly detected recurring charge (seen recently, never alerted before).
        if s.detected and s.last_seen_on and (today - s.last_seen_on).days <= 35:
            out.append({
                "kind": "sub_new",
                # Include the merchant so a reused SQLite rowid (delete the
                # newest sub, detect a different one) can't suppress the alert.
                "dedup_key": f"sub_new:{s.id}:{s.norm_merchant or s.name}",
                "title": f"New recurring charge: {name}",
                "body": (f"About {_money(s.monthly_cents)}/mo. Tally spotted it "
                         f"as recurring."),
                "severity": "info",
            })
    return out


def _weekly_candidates(session: Session, today: date) -> list[dict]:
    iso = today.isocalendar()  # (year, week, weekday)
    week_start = today - timedelta(days=6)  # trailing 7 days, inclusive
    lex = instance_lexicons(session)
    gambling, noncons = lex["gambling"], lex["nonconsumption"]
    txns = session.exec(
        select(Transaction)
        .where(Transaction.posted_date >= week_start)
        .where(Transaction.posted_date <= today)).all()

    week_spent = 0
    by_merchant: dict[str, int] = {}
    for t in txns:
        amt = spend_amount(t, gambling, noncons)
        if amt is None:
            continue
        week_spent += amt
        key = _short(t.raw_description or t.norm_merchant, 28)
        by_merchant[key] = by_merchant.get(key, 0) + amt

    p = compute_pace(session, today)
    pace_phrase = {
        "over": "you're on track to go over your cap",
        "watch": "you're running a little hot",
        "under": "you're on pace",
    }.get(p["state"], "you're on pace")
    top = sorted(by_merchant.items(), key=lambda x: -x[1])[:3]
    top_str = ", ".join(f"{n} {_money(c)}" for n, c in top) if top else "nothing yet"

    body = (f"Spent {_money(week_spent)} over the last 7 days, and {pace_phrase} "
            f"for {p['month']}. Top merchants: {top_str}.")
    return [{
        "kind": "weekly",
        "dedup_key": f"weekly:{iso[0]}-W{iso[1]:02d}",
        "title": "This week in Tally",
        "body": body,
        "severity": "info",
    }]


# ─────────────────────────── evaluate ───────────────────────────

def evaluate_alerts(session: Session, today: date | None = None,
                    deliver: bool = True) -> dict:
    """Build every candidate alert, insert the ones not already logged, and
    deliver the genuinely new ones. Idempotent by dedup_key.

    Seeding is per KIND, not one global first run: the first time a given kind
    ever appears it is logged quietly (marked read, not delivered). This matters
    because subscriptions are detected lazily, so a global "first run" seed can
    be consumed by the always-present weekly rollup before any subscription
    exists; the whole subscription back-catalog would then flood Notification
    Center the first time detection runs. Per-kind seeding keeps that quiet while
    still delivering genuinely new events after a kind is established.
    """
    today = today or date.today()
    existing = set(session.exec(select(Alert.dedup_key)).all())
    seeded_kinds = set(session.exec(select(Alert.kind)).all())

    candidates: list[dict] = []
    candidates += _pace_candidates(session, today)
    candidates += _big_charge_candidates(session, today)
    candidates += _subscription_candidates(session, today)
    candidates += _weekly_candidates(session, today)

    # Keep first occurrence of each dedup_key within this run, drop known ones.
    seen: set[str] = set()
    fresh = []
    for c in candidates:
        k = c["dedup_key"]
        if k in existing or k in seen:
            continue
        seen.add(k)
        fresh.append(c)

    created: list[tuple[Alert, bool]] = []  # (alert, is_seed)
    for c in fresh:
        is_seed = c["kind"] not in seeded_kinds
        a = Alert(kind=c["kind"], dedup_key=c["dedup_key"], title=c["title"],
                  body=c["body"], severity=c.get("severity", "info"),
                  read=is_seed)
        try:
            # A savepoint per row so a concurrent run (daily sync vs midday tick
            # vs the API) that already logged this key is a no-op, not a crash.
            with session.begin_nested():
                session.add(a)
            created.append((a, is_seed))
        except IntegrityError:
            session.expunge(a)
    session.commit()
    for a, _ in created:
        session.refresh(a)

    delivered = 0
    emailed = 0
    if deliver:
        for a, is_seed in created:
            if is_seed:
                continue
            if notify_macos(a.title, a.body):
                a.notified = True
                delivered += 1
            if a.kind == "weekly":
                if send_email("Tally weekly digest", a.body):
                    emailed += 1
        session.commit()

    return {
        "created": len(created),
        "delivered": delivered,
        "emailed": emailed,
        "seeded": any(is_seed for _, is_seed in created),
        "alerts": [_alert_out(a) for a, _ in created],
    }


# ─────────────────────────── API ───────────────────────────

def _alert_out(a: Alert) -> dict:
    return {"id": a.id, "kind": a.kind, "title": a.title, "body": a.body,
            "severity": a.severity, "read": a.read, "notified": a.notified,
            "created_at": a.created_at.isoformat() if a.created_at else None}


@router.get("/alerts")
def list_alerts(limit: int = 50) -> dict:
    limit = max(1, min(200, limit))
    with Session(engine) as s:
        rows = s.exec(select(Alert)
                      .order_by(Alert.created_at.desc(), Alert.id.desc())
                      .limit(limit)).all()
        unread = s.exec(select(func.count()).select_from(Alert)
                        .where(Alert.read == False)).one()  # noqa: E712
        return {"alerts": [_alert_out(a) for a in rows], "unread": unread,
                "smtp_configured": settings.smtp_configured,
                "notify": settings.ALERTS_NOTIFY}


@router.post("/alerts/evaluate")
def api_evaluate() -> dict:
    with Session(engine) as s:
        return evaluate_alerts(s, deliver=True)


@router.post("/alerts/{alert_id}/read")
def mark_read(alert_id: int) -> dict:
    with Session(engine) as s:
        a = s.get(Alert, alert_id)
        if a is None:
            raise HTTPException(404, "alert not found")
        a.read = True
        s.add(a)
        s.commit()
        return {"ok": True, "id": alert_id}


@router.post("/alerts/read-all")
def mark_all_read() -> dict:
    with Session(engine) as s:
        n = 0
        for a in s.exec(select(Alert).where(Alert.read == False)).all():  # noqa: E712
            a.read = True
            s.add(a)
            n += 1
        s.commit()
        return {"ok": True, "marked": n}
