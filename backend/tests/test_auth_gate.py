"""Every registered route must be behind the auth wall when auth is on.

This walks the live route table, so a new endpoint added without thinking
about auth fails here unless it is deliberately put on the public allowlist.
"""

from __future__ import annotations

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app
from app.security import is_public_path

# The complete set of paths that may answer without a session. Additions to
# this list are a security decision; make them consciously.
EXPECTED_PUBLIC = {
    "/", "/healthz", "/favicon.ico",
    "/api/auth/me", "/api/auth/register/begin", "/api/auth/register/finish",
    "/api/auth/login/begin", "/api/auth/login/finish", "/api/auth/logout",
    "/api/auth/passkeys", "/api/auth/passkeys/{cred_id}",
    "/api/auth/passkeys/{cred_id}/rename",
}


def _walkable_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            concrete = route.path.replace("{sub_id}", "1").replace("{cred_id}", "1")
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield route.path, concrete, method


def test_allowlist_matches_expectations():
    """is_public_path must agree exactly with the reviewed allowlist above."""
    for pattern, concrete, _ in _walkable_routes():
        assert is_public_path(concrete) == (pattern in EXPECTED_PUBLIC), pattern


def test_every_route_requires_auth(auth_on, client: TestClient):
    for pattern, concrete, method in _walkable_routes():
        r = client.request(method, concrete)
        if pattern in EXPECTED_PUBLIC:
            assert r.status_code != 401, f"{method} {pattern} should be public"
        else:
            assert r.status_code == 401, (
                f"{method} {pattern} answered {r.status_code} without a session")


def test_static_mount_is_public(auth_on, client: TestClient):
    assert client.get("/static/favicon.svg").status_code != 401


def test_events_stream_requires_auth(auth_on, client: TestClient):
    assert client.get("/api/events").status_code == 401


def test_all_reachable_when_auth_off(client: TestClient):
    for _, concrete, method in _walkable_routes():
        if concrete == "/api/events":
            continue  # SSE stream never terminates; covered by the auth-on test
        r = client.request(method, concrete)
        assert r.status_code != 401, f"{method} {concrete} 401 with auth off"


def test_security_headers_present(client: TestClient):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in r.headers
    assert r.headers["referrer-policy"] == "no-referrer"
