"""Tally API.

Serves the dashboard and computes its data live from the SQLite database that
the ingestion pipeline fills. The frontend (frontend/index.html) loads
/budget-data.js and /budget-subs.js, which this module generates on the fly from
real transactions, so the UI always reflects the current database. Uploading a
statement runs the same penny-reconciled pipeline and pushes an SSE event so the
open dashboard refreshes itself.

Money is integer cents in the DB; this layer converts to dollar numbers on the
way out. No floats are stored, only presented.
"""

from __future__ import annotations

import calendar
import json
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from pydantic import BaseModel
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from sqlmodel import Session, func, select

from .canonical import make_txn_uid
from .config import settings
from .db import engine, init_db
from .models import (Account, Budget, Card, IncomeSource, IngestedFile,
                     Subscription, Transaction)
from .ingest.common import period_from_records
from .ingest.convergence import learned_category
from .ingest.pipeline import (ingest_file, sha256_file, ReconcileError,
                              _assign_seq, _ensure_account, _match_transfers)
from .ocr_apple import OCRUnavailable, ocr_image, parse_apple_card

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".heic")

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"

def _month_window(months: int, today: date | None = None) -> list[tuple[int, int]]:
    """The last `months` full calendar months plus the current (partial) month,
    oldest first, as (year, month) pairs."""
    today = today or date.today()
    y, m = today.year, today.month
    out: list[tuple[int, int]] = []
    for _ in range(months + 1):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _period_label(win: list[tuple[int, int]]) -> str:
    (y0, m0), (y1, m1) = win[0], win[-1]
    a, b = calendar.month_abbr[m0], calendar.month_abbr[m1]
    return f"{a}-{b} {y1}" if y0 == y1 else f"{a} {y0} - {b} {y1}"

CAT_LABEL = {
    "dining": "Dining & Delivery", "grocery": "Groceries", "gas": "Gas & EV Charging",
    "apple_hardware": "Apple Hardware (one-time)", "apple_services": "Apple Services",
    "shopping": "Shopping", "entertainment": "Entertainment", "subscriptions": "Subscriptions",
    "fitness": "Fitness", "transit": "Transit & Parking", "drugstore": "Drugstore",
    "streaming": "Streaming", "other": "Other / Misc",
}
# moderate-light monthly targets (dollars); used unless a Budget row overrides
TARGET = {
    "dining": 600, "grocery": 325, "gas": 216, "shopping": 150, "entertainment": 150,
    "subscriptions": 230, "transit": 63, "drugstore": 20, "apple_services": 50,
    "fitness": 0, "streaming": 0, "other": 120, "apple_hardware": 0,
}
GAMBLING = ("kalshi", "prizepicks", "draftkings", "fanduel", "robinhood", "kraken", "coinbase")
DELIVERY = ("doordash", "uber eats", "ubereats")
# Checking-account outflows that are NOT consumption: card payoffs, savings moves,
# tuition, P2P, and internal transfers. These have no matching transfer leg so the
# transfer-matcher misses them; exclude them from the spend/rewards math by description.
NONCONSUMPTION = (
    "credit card auto pay", "credit card retry", "applecard gsbank payment",
    "to wells fargo autograph", "autograph visa", "apple gs savings", "way2save",
    "savings transfer", "tuition", "edu pay", "money transfer authorized", "online transfer",
    "transfer to", "zelle to", "venmo payment", "bill pay",
)
SAVINGS_OPTIONS = [
    {"name": "Apple Savings (Goldman)", "apy": 3.40, "note": "Your current account. Keep it here."},
    {"name": "Wells Fargo savings", "apy": 0.01, "note": "Not worth it."},
    {"name": "Chase savings", "apy": 0.01, "note": "Not worth it."},
    {"name": "CIT Platinum ($5k+)", "apy": 3.75, "note": "Optional upgrade for a long-term bucket."},
    {"name": "Forbright", "apy": 4.15, "note": "Highest mainstream, optional."},
]

INSECURE_SECRETS = {"", "dev-insecure-change-me", "change-me-to-a-long-random-string"}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if settings.AUTH_ENABLED and settings.SESSION_SECRET in INSECURE_SECRETS:
        raise RuntimeError(
            "AUTH_ENABLED=true requires a real SESSION_SECRET in .env "
            "(e.g. `openssl rand -hex 32`); refusing to start with the "
            "default one because it would make sessions forgeable."
        )
    init_db()
    ensure_bootstrap_code()
    yield


app = FastAPI(title="Tally", lifespan=_lifespan)

from starlette.middleware.sessions import SessionMiddleware  # noqa: E402
from .events import hub  # noqa: E402
from .security import AuthGateMiddleware, SecurityHeadersMiddleware  # noqa: E402
from .plaid_link import router as plaid_router  # noqa: E402
from .auth import (router as auth_router, require_user,  # noqa: E402
                   ensure_bootstrap_code)

# Middleware nesting (first added runs innermost): the session cookie must be
# decoded before the auth gate reads it, so SessionMiddleware is added last.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(AuthGateMiddleware)
app.add_middleware(SessionMiddleware, secret_key=settings.SESSION_SECRET,
                   same_site="lax", https_only=settings.COOKIE_SECURE,
                   max_age=settings.SESSION_MAX_AGE_DAYS * 86400)
app.include_router(plaid_router)
app.include_router(auth_router)

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# ───────────────────────────── helpers ─────────────────────────────
def _card_key(account: Account) -> str:
    """Explicit account -> rewards card mapping; None means earns nothing."""
    return account.card_key or "debit"


def _card_rules(session: Session) -> dict[str, dict]:
    rules = {}
    for c in session.exec(select(Card)).all():
        try:
            rules[c.key] = json.loads(c.rules_json or "{}")
        except json.JSONDecodeError:
            rules[c.key] = {}
    return rules


def _best_rate(rules: dict, category: str) -> tuple[str, int]:
    best_key, best = "chase", -1
    for key in ("apple", "wf_autograph", "chase"):
        r = rules.get(key, {}).get(category, 0)
        if r > best:
            best, best_key = r, key
    return best_key, best


def compute_dashboard(session: Session, months: int = 6) -> dict:
    """Aggregate the dashboard over a rolling window: the last `months` full
    calendar months plus the current partial month. Monthly averages divide by
    the number of full window months that actually have data, so a young
    database is not diluted toward zero and the current month never skews the
    average downward."""
    win = _month_window(months)
    window_start = date(win[0][0], win[0][1], 1)
    win_index = {ym: i for i, ym in enumerate(win)}

    txns = session.exec(select(Transaction)
                        .where(Transaction.posted_date >= window_start)).all()
    accounts = {a.id: a for a in session.exec(select(Account)).all()}
    rules = _card_rules(session)
    budget_rows = {b.category: b.target_cents for b in session.exec(select(Budget)).all()}

    cat_total = defaultdict(int)            # category -> spend cents (positive)
    cat_card = defaultdict(lambda: defaultdict(int))
    trend = [0] * len(win)
    trend_deliv = [0] * len(win)
    card_total = defaultdict(int)
    deliv_total, deliv_n = 0, 0
    gamb_total = 0
    gamb_merchants: dict[str, int] = {}
    gap_by_cat: dict[str, float] = defaultdict(float)
    gap_card_for: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rewards_now = 0.0
    rewards_opt = 0.0
    months_with_data: set[tuple[int, int]] = set()

    for t in txns:
        if t.is_transfer:
            continue
        ym = (t.posted_date.year, t.posted_date.month)
        bucket = win_index.get(ym)
        if bucket is None:
            continue  # future-dated row; not in the window
        desc = (t.norm_merchant + " " + t.raw_description).lower()
        if any(g in desc for g in GAMBLING):
            if t.amount_cents < 0:
                gamb_total += -t.amount_cents
                gamb_merchants[t.norm_merchant] = gamb_merchants.get(t.norm_merchant, 0) + 1
            continue
        if t.amount_cents >= 0:
            continue  # inflow, not consumption
        if any(k in desc for k in NONCONSUMPTION):
            continue  # card payoff / savings / tuition / P2P / transfer, not spend
        months_with_data.add(ym)
        spend = -t.amount_cents
        cat = t.category or "other"
        acc = accounts.get(t.account_id)
        ck = _card_key(acc) if acc else "debit"
        cat_total[cat] += spend
        cat_card[cat][ck] += spend
        card_total[ck] += spend
        trend[bucket] += spend
        if any(d in desc for d in DELIVERY):
            trend_deliv[bucket] += spend
            deliv_total += spend
            deliv_n += 1
        # rewards: current vs best card rate (bps)
        now_rate = rules.get(ck, {}).get(cat, 0)
        _, best = _best_rate(rules, cat)
        rewards_now += spend * now_rate / 10000.0
        rewards_opt += spend * best * 1.0 / 10000.0
        if best > now_rate:
            gap_by_cat[cat] += spend * (best - now_rate) / 10000.0
            gap_card_for[cat][ck] += spend

    # Full window months (everything but the trailing partial) that have data.
    n = float(max(1, sum(1 for ym in win[:-1] if ym in months_with_data)))
    total = sum(cat_total.values())
    categories = []
    for cid, cents in sorted(cat_total.items(), key=lambda x: -x[1]):
        bk, _ = _best_rate(rules, cid)
        tgt = budget_rows.get(cid)
        target = round(tgt / 100, 2) if tgt is not None else TARGET.get(cid, round(cents / n / 100))
        categories.append({
            "id": cid, "label": CAT_LABEL.get(cid, cid.replace("_", " ").title()),
            "total6": round(cents / 100, 2), "monthly": round(cents / n / 100, 2),
            "best_card": bk, "target": target,
        })

    _top = max(gap_by_cat, key=gap_by_cat.get) if gap_by_cat else None
    _topcard = (max(gap_card_for[_top], key=gap_card_for[_top].get)
                if _top and gap_card_for[_top] else None)
    _cardname = {"apple": "the Apple Card", "wf_autograph": "WF Autograph", "chase": "Chase", "debit": "debit"}
    return {
        "meta": {"period": _period_label(win),
                 "months": [calendar.month_abbr[m] for _, m in win],
                 "window_months": months,
                 "full_months_with_data": int(n),
                 "ocr_unreconciled": session.exec(
                     select(func.count()).select_from(Transaction)
                     .where(Transaction.origin == "ocr")).one(),
                 "generated": date.today().isoformat()},
        "spend": {
            "monthly_avg": round(total / n / 100, 2), "total6": round(total / 100, 2),
            "trend": [round(c / 100, 2) for c in trend],
            "trend_delivery": [round(c / 100, 2) for c in trend_deliv],
            "apple_hardware_onetime": round(cat_total.get("apple_hardware", 0) / 100, 2),
        },
        "categories": categories,
        "delivery": {"monthly": round(deliv_total / n / 100, 2), "total6": round(deliv_total / 100, 2),
                     "orders": deliv_n, "avg_order": round(deliv_total / deliv_n / 100, 2) if deliv_n else 0},
        "cards": {"apple": round(card_total.get("apple", 0) / n / 100, 2),
                  "wf_autograph": round(card_total.get("wf_autograph", 0) / n / 100, 2),
                  "chase": round(card_total.get("chase", 0) / n / 100, 2),
                  "debit": round(card_total.get("debit", 0) / n / 100, 2)},
        "rewards": {"now_yr": round(rewards_now / n * 12 / 100, 2),
                    "optimal_yr": round(rewards_opt / n * 12 / 100, 2),
                    "gap_yr": round((rewards_opt - rewards_now) / n * 12 / 100, 2),
                    "top_label": CAT_LABEL.get(_top, _top) if _top else None,
                    "top_card": _cardname.get(_topcard, _topcard) if _topcard else None},
        "gambling": {"monthly": round(gamb_total / n / 100, 2), "total6": round(gamb_total / 100, 2),
                     "merchants": [m for m, _ in sorted(gamb_merchants.items(), key=lambda x: -x[1])[:4]]},
        "savings_options": SAVINGS_OPTIONS,
    }


def compute_subs(session: Session) -> list[dict]:
    out = []
    for s in session.exec(select(Subscription)).all():
        out.append({
            "name": s.name, "monthly": round(s.monthly_cents / 100, 2), "category": s.category,
            "current_card": s.current_card or "", "recommended_card": s.recommended_card or "",
            "status": s.status, "manage_url": s.manage_url or "", "moved": s.moved, "id": s.id,
        })
    return out


# ───────────────────────────── data endpoints ─────────────────────────────
@app.get("/budget-data.js")
def budget_data_js(_user=Depends(require_user)) -> Response:
    with Session(engine) as session:
        data = compute_dashboard(session)
    body = "window.BUDGET_DATA = " + json.dumps(data) + ";\n"
    return Response(content=body, media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


@app.get("/budget-subs.js")
def budget_subs_js(_user=Depends(require_user)) -> Response:
    with Session(engine) as session:
        subs = compute_subs(session)
    body = "window.BUDGET_SUBS = " + json.dumps(subs) + ";\n"
    return Response(content=body, media_type="application/javascript",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/overview")
def api_overview(months: int = 6, _user=Depends(require_user)) -> dict:
    months = max(3, min(24, months))
    with Session(engine) as session:
        return compute_dashboard(session, months=months)


@app.get("/api/subscriptions")
def api_subs(_user=Depends(require_user)) -> list[dict]:
    with Session(engine) as session:
        return compute_subs(session)


@app.post("/api/subscriptions/{sub_id}/move")
def api_move(sub_id: int, moved: bool = True, _user=Depends(require_user)) -> dict:
    with Session(engine) as session:
        s = session.get(Subscription, sub_id)
        if not s:
            raise HTTPException(404, "subscription not found")
        s.moved = moved
        session.add(s)
        session.commit()
        return {"id": sub_id, "moved": moved}


@app.get("/api/accounts")
def api_accounts(_user=Depends(require_user)) -> list[dict]:
    with Session(engine) as session:
        return [{"id": a.id, "name": a.name, "kind": a.kind,
                 "balance": round(a.balance_cents / 100, 2), "apy": a.apy_bps / 100}
                for a in session.exec(select(Account)).all()]


def _is_image_upload(file: UploadFile) -> bool:
    """An Apple Card screenshot, by content type or file extension."""
    ctype = (file.content_type or "").lower()
    if ctype.startswith("image/"):
        return True
    name = (file.filename or "").lower()
    return name.endswith(IMAGE_EXTS)


# OCR text cannot be penny-reconciled (there is no printed total to check),
# so screenshot ingestion is a two-step ceremony: parse to a PREVIEW the user
# reviews, then an explicit confirm writes the rows, flagged origin='ocr'
# with an unreconciled IngestedFile audit row.
_OCR_PENDING: dict[str, dict] = {}  # token -> {records, hash, name, ts}
_OCR_TTL_SECONDS = 1800


def _prune_ocr_pending() -> None:
    cutoff = time.time() - _OCR_TTL_SECONDS
    for token in [t for t, p in _OCR_PENDING.items() if p["ts"] < cutoff]:
        _OCR_PENDING.pop(token, None)


def _ocr_preview(dest: Path) -> JSONResponse:
    text = ocr_image(str(dest))
    records = parse_apple_card(text)
    file_hash = sha256_file(str(dest))
    _assign_seq(records)
    preview = []
    new_count = 0
    with Session(engine) as session:
        if session.get(IngestedFile, file_hash) is not None:
            return JSONResponse(content={"ok": True, "source": "ocr",
                                         "duplicate": True, "added": 0})
        for r in records:
            exists = session.get(Transaction, r.txn_uid()) is not None
            preview.append({
                "date": r.posted_date.isoformat(),
                "merchant": r.raw_description,
                "amount": round(r.amount_cents / 100, 2),
                "category": r.category,
                "exists": exists,
            })
            if not exists:
                new_count += 1
    _prune_ocr_pending()
    token = secrets.token_urlsafe(16)
    _OCR_PENDING[token] = {"records": records, "hash": file_hash,
                           "name": dest.name, "ts": time.time()}
    return JSONResponse(content={
        "ok": True, "source": "ocr", "needs_confirm": True, "token": token,
        "preview": preview, "new": new_count,
        "note": "OCR rows are not penny-reconciled; review before confirming.",
    })


class ConfirmBody(BaseModel):
    token: str


@app.post("/api/ingest/confirm")
def api_ingest_confirm(body: ConfirmBody, _user=Depends(require_user)) -> JSONResponse:
    _prune_ocr_pending()
    pending = _OCR_PENDING.pop(body.token, None)
    if pending is None:
        return JSONResponse(status_code=410,
                            content={"ok": False, "error": "preview_expired",
                                     "detail": "Preview expired; upload the screenshot again."})
    records = pending["records"]
    with Session(engine) as session:
        if session.get(IngestedFile, pending["hash"]) is not None:
            return JSONResponse(content={"ok": True, "duplicate": True, "added": 0})
        account_id = _ensure_account(session, "apple")
        added = 0
        for r in records:
            uid = r.txn_uid()
            if session.get(Transaction, uid) is not None:
                continue
            learned = learned_category(session, r.norm_merchant)
            session.add(Transaction(
                txn_uid=uid,
                account_id=account_id,
                posted_date=r.posted_date,
                amount_cents=r.amount_cents,
                raw_description=r.raw_description,
                norm_merchant=r.norm_merchant,
                category=learned or r.category,
                category_source="learned" if learned else r.category_source,
                is_transfer=r.is_transfer,
                transfer_group_id=r.transfer_group_id,
                source_file_hash=pending["hash"],
                source_statement_id=r.source_statement_id,
                source_line=r.source_line,
                origin="ocr",
            ))
            added += 1
        session.add(IngestedFile(
            file_sha256=pending["hash"],
            account="apple",
            period=period_from_records(records) if records else None,
            row_count=len(records),
            reconciled=False,  # OCR has no totals to reconcile against
        ))
        session.commit()
        _match_transfers(session)
    hub.publish("transactions:updated")
    return JSONResponse(content={"ok": True, "added": added})


def _safe_filename(raw: str | None) -> str:
    """Client filenames are attacker-controlled; reduce to a boring basename."""
    name = Path(raw or "upload").name  # strips any directory components
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(". ")
    return name[:128] or "upload"


async def _save_upload(file: UploadFile, dest: Path) -> None:
    """Stream to disk, enforcing the size cap without buffering in memory."""
    limit = settings.MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    413, f"file exceeds the {settings.MAX_UPLOAD_MB} MB upload limit")
            out.write(chunk)


@app.post("/api/ingest")
async def api_ingest(file: UploadFile = File(...), _user=Depends(require_user)) -> JSONResponse:
    dest_dir = Path(settings.DATA_DIR) / "statements"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _safe_filename(file.filename)
    await _save_upload(file, dest)

    # Image upload: Apple Card has no aggregator, so users snap the Wallet app
    # transaction list. OCR it on-device and return a preview; rows are only
    # written when /api/ingest/confirm is called with the returned token.
    if _is_image_upload(file):
        try:
            return _ocr_preview(dest)
        except OCRUnavailable as e:
            return JSONResponse(status_code=422,
                                content={"ok": False, "error": "ocr_unavailable", "detail": str(e)})

    try:
        with Session(engine) as session:
            result = ingest_file(str(dest), session=session)
            session.commit()
    except ReconcileError as e:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": "reconcile_failed", "detail": str(e)})
    except ValueError as e:
        return JSONResponse(status_code=415,
                            content={"ok": False, "error": "unsupported_format", "detail": str(e)})
    except FileNotFoundError as e:
        # pdftotext (poppler) missing on this host
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": "missing_dependency",
                     "detail": "PDF parsing needs pdftotext: brew install poppler "
                               f"(macOS) or apt install poppler-utils (Linux). [{e}]"})
    hub.publish("transactions:updated")
    return JSONResponse(content={"ok": True, "result": result})


@app.get("/api/events")
async def api_events(_user=Depends(require_user)):
    q = hub.subscribe()

    async def gen():
        try:
            while True:
                event = await q.get()
                yield {"event": "message", "data": event}
        finally:
            hub.unsubscribe(q)

    return EventSourceResponse(gen())


# ───────────────────────────── frontend ─────────────────────────────
@app.get("/")
def index() -> FileResponse:
    idx = FRONTEND / "index.html"
    if not idx.exists():
        raise HTTPException(404, "frontend not built")
    return FileResponse(idx, headers={"Cache-Control": "no-store"})


# serve any other static frontend assets (css/js) if present
if FRONTEND.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
