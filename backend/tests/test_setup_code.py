"""Registration bootstrap: no passkey enrollment without a valid setup code."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.auth import _hash_code, _utcnow, issue_setup_code
from app.db import engine
from app.models import SetupCode, User

ORIGIN = {"Origin": "http://localhost:8787"}


def test_register_begin_requires_code(auth_on, client: TestClient):
    r = client.post("/api/auth/register/begin", json={"handle": "intruder"},
                    headers=ORIGIN)
    assert r.status_code == 401


def test_register_begin_rejects_bad_code(auth_on, client: TestClient):
    r = client.post("/api/auth/register/begin",
                    json={"handle": "intruder", "setup_code": "AAAA-AAAA-AAAA-AAAA"},
                    headers=ORIGIN)
    assert r.status_code == 401


def test_register_begin_rejects_expired_code(auth_on, client: TestClient):
    with Session(engine) as s:
        code = issue_setup_code(s)
        row = s.exec(select(SetupCode)
                     .where(SetupCode.code_hash == _hash_code(code))).one()
        row.expires_at = _utcnow() - timedelta(minutes=1)
        s.add(row)
        s.commit()
    r = client.post("/api/auth/register/begin",
                    json={"handle": "late", "setup_code": code}, headers=ORIGIN)
    assert r.status_code == 401


def test_register_begin_accepts_valid_code_and_creates_no_user(auth_on, client: TestClient):
    with Session(engine) as s:
        before = len(s.exec(select(User)).all())
        code = issue_setup_code(s)
    r = client.post("/api/auth/register/begin",
                    json={"handle": "owner", "setup_code": code}, headers=ORIGIN)
    assert r.status_code == 200
    assert "challenge" in r.json()
    with Session(engine) as s:
        # The ceremony has not finished; no User row may exist yet.
        assert len(s.exec(select(User)).all()) == before


def test_register_begin_rejects_unknown_origin(auth_on, client: TestClient):
    with Session(engine) as s:
        code = issue_setup_code(s)
    r = client.post("/api/auth/register/begin",
                    json={"handle": "owner", "setup_code": code},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 400


def test_register_begin_rejects_ip_origin(auth_on, client: TestClient):
    """127.0.0.1 cannot be a WebAuthn RP ID; the error must say to use localhost."""
    with Session(engine) as s:
        code = issue_setup_code(s)
    r = client.post("/api/auth/register/begin",
                    json={"handle": "owner", "setup_code": code},
                    headers={"Origin": "http://127.0.0.1:8787"})
    assert r.status_code == 400
    assert "localhost" in r.json()["detail"]


def test_login_begin_with_no_credentials_is_404(auth_on, client: TestClient):
    r = client.post("/api/auth/login/begin", json={}, headers=ORIGIN)
    assert r.status_code == 404


def test_me_reports_state(auth_on, client: TestClient):
    r = client.get("/api/auth/me")
    assert r.json() == {"authEnabled": True, "authenticated": False,
                        "anyCredentials": False}
