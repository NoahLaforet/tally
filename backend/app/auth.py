"""Passkey (WebAuthn) authentication.

A complete registration + login ceremony using the `webauthn` library, with the
challenge held in the signed session cookie and credentials stored in SQLite.

It is gated by Settings.AUTH_ENABLED. When off (the default), the app runs as a
single local user with no login wall, but every endpoint and the require_user
dependency still exist, so turning it on (AUTH_ENABLED=true) enforces login on
the protected data routes without any code change. Set a real SESSION_SECRET and
the RP_ID / ORIGIN for your host when enabling it for remote/multi-user use.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
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
from .models import Credential, User

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _user_by_handle(session: Session, handle: str) -> User | None:
    return session.exec(select(User).where(User.handle == handle)).first()


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
    uid = request.session.get("user_id")
    if not uid:
        return {"authEnabled": True, "authenticated": False}
    with Session(engine) as s:
        u = s.get(User, uid)
        return {"authEnabled": True, "authenticated": True, "handle": u.handle if u else None}


@router.post("/register/begin")
async def register_begin(request: Request) -> Response:
    body = await request.json()
    handle = (body.get("handle") or "").strip().lower()
    if not handle:
        raise HTTPException(400, "handle required")
    with Session(engine) as s:
        user = _user_by_handle(s, handle)
        if user is None:
            user = User(handle=handle, display_name=body.get("display_name") or handle)
            s.add(user)
            s.commit()
            s.refresh(user)
        existing = s.exec(select(Credential).where(Credential.user_id == user.id)).all()
        exclude = [PublicKeyCredentialDescriptor(id=c.credential_id) for c in existing]
        uid = user.id
    opts = generate_registration_options(
        rp_id=settings.RP_ID,
        rp_name=settings.RP_NAME,
        user_name=handle,
        user_id=str(uid).encode(),
        user_display_name=body.get("display_name") or handle,
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    request.session["reg_challenge"] = bytes_to_base64url(opts.challenge)
    request.session["reg_uid"] = uid
    return Response(options_to_json(opts), media_type="application/json")


@router.post("/register/finish")
async def register_finish(request: Request) -> JSONResponse:
    body = await request.json()
    challenge_b64 = request.session.get("reg_challenge")
    uid = request.session.get("reg_uid")
    if not challenge_b64 or not uid:
        raise HTTPException(400, "no registration in progress")
    try:
        v = verify_registration_response(
            credential=json.dumps(body),
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=settings.RP_ID,
            expected_origin=settings.ORIGIN,
        )
    except Exception as e:  # noqa: BLE001 - surface a clean error, no internals
        raise HTTPException(400, "registration verification failed") from e
    with Session(engine) as s:
        s.add(Credential(user_id=uid, credential_id=v.credential_id,
                         public_key=v.credential_public_key, sign_count=v.sign_count))
        s.commit()
    request.session.pop("reg_challenge", None)
    request.session.pop("reg_uid", None)
    request.session["user_id"] = uid
    return JSONResponse({"ok": True})


@router.post("/login/begin")
async def login_begin(request: Request) -> Response:
    body = await request.json()
    handle = (body.get("handle") or "").strip().lower()
    allow: list[PublicKeyCredentialDescriptor] = []
    if handle:
        with Session(engine) as s:
            user = _user_by_handle(s, handle)
            if user:
                for c in s.exec(select(Credential).where(Credential.user_id == user.id)).all():
                    allow.append(PublicKeyCredentialDescriptor(id=c.credential_id))
    opts = generate_authentication_options(
        rp_id=settings.RP_ID,
        allow_credentials=allow or None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    request.session["auth_challenge"] = bytes_to_base64url(opts.challenge)
    return Response(options_to_json(opts), media_type="application/json")


@router.post("/login/finish")
async def login_finish(request: Request) -> JSONResponse:
    body = await request.json()
    challenge_b64 = request.session.get("auth_challenge")
    if not challenge_b64:
        raise HTTPException(400, "no login in progress")
    raw_id = base64url_to_bytes(body.get("rawId") or body.get("id") or "")
    with Session(engine) as s:
        cred = s.exec(select(Credential).where(Credential.credential_id == raw_id)).first()
        if not cred:
            raise HTTPException(400, "unknown credential")
        try:
            v = verify_authentication_response(
                credential=json.dumps(body),
                expected_challenge=base64url_to_bytes(challenge_b64),
                expected_rp_id=settings.RP_ID,
                expected_origin=settings.ORIGIN,
                credential_public_key=cred.public_key,
                credential_current_sign_count=cred.sign_count,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, "login verification failed") from e
        cred.sign_count = v.new_sign_count
        s.add(cred)
        s.commit()
        uid = cred.user_id
    request.session.pop("auth_challenge", None)
    request.session["user_id"] = uid
    return JSONResponse({"ok": True})


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    request.session.clear()
    return JSONResponse({"ok": True})
