"""Passkey (WebAuthn) authentication.

A complete registration + login ceremony using the `webauthn` library, with the
challenge held in the signed session cookie and credentials stored in SQLite.

Gated by Settings.AUTH_ENABLED. When off (the default), the app runs as a
single local user with no login wall. When on:

- Registering a passkey ALWAYS requires either an authenticated session or a
  one-time setup code. The first code is printed to the server console on
  startup; later codes come from `uv run python -m app.newcode`. A fresh
  instance can therefore never be claimed by a stranger who can reach the port.
- This is a single-user instance: once a user exists, every new passkey
  attaches to that user. No User row is created until a registration ceremony
  completes.
- Passkeys are origin-bound. The RP ID is derived from the request's Origin
  header and validated against the configured origins, so passkeys work on
  both http://localhost:8787 and e.g. a Tailscale HTTPS hostname; each origin
  needs its own registered passkey. (An IP origin like http://127.0.0.1 can
  never host a passkey; the spec forbids IP RP IDs.)
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlmodel import Session, select
from webauthn import (generate_authentication_options,
                      generate_registration_options, options_to_json,
                      verify_authentication_response,
                      verify_registration_response)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                       PublicKeyCredentialDescriptor,
                                       ResidentKeyRequirement,
                                       UserVerificationRequirement)

from .config import settings
from .db import engine
from .models import Credential, SetupCode, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

SETUP_CODE_TTL = timedelta(minutes=30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def _new_code() -> str:
    # 4x4 uppercase base32-ish, easy to read off a terminal.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def issue_setup_code(session: Session) -> str:
    """Create, persist (hashed), and return a fresh one-time setup code."""
    code = _new_code()
    session.add(SetupCode(code_hash=_hash_code(code),
                          expires_at=_utcnow() + SETUP_CODE_TTL))
    session.commit()
    return code


def _valid_code_row(session: Session, code: str) -> SetupCode | None:
    row = session.exec(select(SetupCode)
                       .where(SetupCode.code_hash == _hash_code(code))).first()
    if row is None or row.used_at is not None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < _utcnow():
        return None
    return row


def ensure_bootstrap_code() -> None:
    """On startup with auth enabled and no passkeys yet, print a setup code."""
    if not settings.AUTH_ENABLED:
        return
    with Session(engine) as s:
        if s.exec(select(Credential)).first() is not None:
            return
        code = issue_setup_code(s)
    print(
        "\n"
        "  ┌─────────────────────────────────────────────────────────────┐\n"
        "  │  Tally auth is ON and no passkey is registered yet.         │\n"
        f"  │  One-time setup code (valid 30 min):  {code}   │\n"
        "  │  Open the app, choose 'Register passkey', enter this code.  │\n"
        "  │  Need another later?  uv run python -m app.newcode          │\n"
        "  └─────────────────────────────────────────────────────────────┘\n",
        flush=True,
    )


def _request_origin(request: Request) -> str:
    origin = (request.headers.get("origin") or "").rstrip("/")
    if not origin:
        raise HTTPException(400, "missing Origin header")
    if origin not in settings.allowed_origins:
        raise HTTPException(400, "origin not allowed; add it to EXTRA_ORIGINS")
    return origin


def _request_rp_id(request: Request) -> tuple[str, str]:
    """Validated (origin, rp_id) for this request."""
    origin = _request_origin(request)
    host = urlsplit(origin).hostname or ""
    if host not in settings.allowed_rp_ids:
        raise HTTPException(
            400,
            "passkeys cannot be used on this host; open the app at "
            "http://localhost:8787 instead",
        )
    return origin, host


def require_user(request: Request) -> int | None:
    """Dependency: allow when auth is disabled; otherwise require a session user."""
    if not settings.AUTH_ENABLED:
        return None
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="login required")
    return uid


@router.get("/me")
def me(request: Request) -> dict:
    if not settings.AUTH_ENABLED:
        return {"authEnabled": False, "authenticated": True}
    with Session(engine) as s:
        any_creds = s.exec(select(Credential)).first() is not None
        uid = request.session.get("user_id")
        if not uid:
            return {"authEnabled": True, "authenticated": False,
                    "anyCredentials": any_creds}
        u = s.get(User, uid)
        return {"authEnabled": True, "authenticated": True,
                "anyCredentials": any_creds, "handle": u.handle if u else None}


@router.post("/register/begin")
async def register_begin(request: Request) -> Response:
    body = await request.json()
    origin, rp_id = _request_rp_id(request)

    with Session(engine) as s:
        owner = s.exec(select(User)).first()
        authed = settings.AUTH_ENABLED and request.session.get("user_id")
        code_hash = None
        if not authed:
            code = (body.get("setup_code") or "").strip()
            if not code:
                raise HTTPException(401, "setup code required")
            row = _valid_code_row(s, code)
            if row is None:
                raise HTTPException(401, "invalid or expired setup code")
            code_hash = row.code_hash

        if owner is not None:
            # Single-user instance: every new passkey belongs to the owner.
            uid, handle, display = owner.id, owner.handle, owner.display_name
        else:
            uid = None
            handle = (body.get("handle") or "owner").strip().lower() or "owner"
            display = (body.get("display_name") or handle).strip() or handle

        exclude = []
        if uid is not None:
            existing = s.exec(select(Credential)
                              .where(Credential.user_id == uid)
                              .where(Credential.rp_id == rp_id)).all()
            exclude = [PublicKeyCredentialDescriptor(id=c.credential_id)
                       for c in existing]

    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=settings.RP_NAME,
        user_name=handle,
        # Stable opaque id; the handle is unique in this single-user model.
        user_id=f"tally:{handle}".encode(),
        user_display_name=display,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    request.session["reg"] = {
        "challenge": bytes_to_base64url(opts.challenge),
        "origin": origin, "rp_id": rp_id,
        "uid": uid, "handle": handle, "display": display,
        "code_hash": code_hash,
    }
    return Response(options_to_json(opts), media_type="application/json")


@router.post("/register/finish")
async def register_finish(request: Request) -> JSONResponse:
    body = await request.json()
    pending = request.session.get("reg")
    if not pending:
        raise HTTPException(400, "no registration in progress")
    try:
        v = verify_registration_response(
            credential=json.dumps(body.get("credential") or body),
            expected_challenge=base64url_to_bytes(pending["challenge"]),
            expected_rp_id=pending["rp_id"],
            expected_origin=pending["origin"],
        )
    except Exception as e:  # noqa: BLE001 - surface a clean error, no internals
        raise HTTPException(400, "registration verification failed") from e

    label = (body.get("label") or "").strip()[:64]
    with Session(engine) as s:
        # Burn the setup code now, atomically with credential creation.
        if pending.get("code_hash"):
            row = s.exec(select(SetupCode)
                         .where(SetupCode.code_hash == pending["code_hash"])).first()
            if row is None or row.used_at is not None:
                raise HTTPException(401, "setup code already used")
            row.used_at = _utcnow()
            s.add(row)
        uid = pending.get("uid")
        if uid is None:
            owner = s.exec(select(User)).first()
            if owner is not None:
                uid = owner.id  # raced with another registration; attach
            else:
                user = User(handle=pending["handle"],
                            display_name=pending["display"])
                s.add(user)
                s.flush()
                uid = user.id
        s.add(Credential(user_id=uid, credential_id=v.credential_id,
                         public_key=v.credential_public_key,
                         sign_count=v.sign_count, rp_id=pending["rp_id"],
                         label=label))
        s.commit()
    request.session.pop("reg", None)
    request.session["user_id"] = uid
    return JSONResponse({"ok": True})


@router.post("/login/begin")
async def login_begin(request: Request) -> Response:
    origin, rp_id = _request_rp_id(request)
    allow: list[PublicKeyCredentialDescriptor] = []
    with Session(engine) as s:
        for c in s.exec(select(Credential).where(Credential.rp_id == rp_id)).all():
            allow.append(PublicKeyCredentialDescriptor(id=c.credential_id))
    if not allow:
        raise HTTPException(
            404,
            "no passkey is registered for this host yet; register one with a "
            "setup code",
        )
    opts = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session["auth"] = {
        "challenge": bytes_to_base64url(opts.challenge),
        "origin": origin, "rp_id": rp_id,
    }
    return Response(options_to_json(opts), media_type="application/json")


@router.post("/login/finish")
async def login_finish(request: Request) -> JSONResponse:
    body = await request.json()
    pending = request.session.get("auth")
    if not pending:
        raise HTTPException(400, "no login in progress")
    raw_id = base64url_to_bytes(body.get("rawId") or body.get("id") or "")
    with Session(engine) as s:
        cred = s.exec(select(Credential)
                      .where(Credential.credential_id == raw_id)
                      .where(Credential.rp_id == pending["rp_id"])).first()
        if not cred:
            raise HTTPException(400, "unknown credential")
        try:
            v = verify_authentication_response(
                credential=json.dumps(body),
                expected_challenge=base64url_to_bytes(pending["challenge"]),
                expected_rp_id=pending["rp_id"],
                expected_origin=pending["origin"],
                credential_public_key=cred.public_key,
                credential_current_sign_count=cred.sign_count,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, "login verification failed") from e
        cred.sign_count = v.new_sign_count
        s.add(cred)
        s.commit()
        uid = cred.user_id
    request.session.pop("auth", None)
    request.session["user_id"] = uid
    return JSONResponse({"ok": True})


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})


# ── passkey management (authenticated) ──────────────────────────────────────
def _require_session_user(request: Request) -> int:
    uid = request.session.get("user_id")
    if settings.AUTH_ENABLED and not uid:
        raise HTTPException(401, "login required")
    return uid


@router.get("/passkeys")
def list_passkeys(request: Request) -> list[dict]:
    _require_session_user(request)
    with Session(engine) as s:
        return [{"id": c.id, "label": c.label or f"passkey {c.id}",
                 "rp_id": c.rp_id,
                 "created_at": c.created_at.isoformat() if c.created_at else None}
                for c in s.exec(select(Credential)).all()]


@router.post("/passkeys/{cred_id}/rename")
async def rename_passkey(cred_id: int, request: Request) -> dict:
    _require_session_user(request)
    body = await request.json()
    label = (body.get("label") or "").strip()[:64]
    with Session(engine) as s:
        c = s.get(Credential, cred_id)
        if not c:
            raise HTTPException(404, "no such passkey")
        c.label = label
        s.add(c)
        s.commit()
    return {"ok": True, "id": cred_id, "label": label}


@router.delete("/passkeys/{cred_id}")
def delete_passkey(cred_id: int, request: Request) -> dict:
    _require_session_user(request)
    with Session(engine) as s:
        c = s.get(Credential, cred_id)
        if not c:
            raise HTTPException(404, "no such passkey")
        total = len(s.exec(select(Credential)).all())
        if settings.AUTH_ENABLED and total <= 1:
            raise HTTPException(
                409,
                "refusing to delete the last passkey; register another first "
                "(uv run python -m app.newcode) or set AUTH_ENABLED=false",
            )
        s.delete(c)
        s.commit()
    return {"ok": True, "deleted": cred_id}
