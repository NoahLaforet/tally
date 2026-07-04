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
# The /api/auth/passkeys* endpoints sit under the public /api/auth/ prefix
# but enforce their own session guard (auth._require_session_user), so the
# middleware allowlist marks them public while the auth-on walk expects 401.
EXPECTED_PUBLIC = {
    "/", "/healthz", "/favicon.ico",
    "/api/auth/me", "/api/auth/register/begin", "/api/auth/register/finish",
    "/api/auth/login/begin", "/api/auth/login/finish", "/api/auth/logout",
    "/api/auth/passkeys", "/api/auth/passkeys/{cred_id}",
    "/api/auth/passkeys/{cred_id}/rename",
}
SELF_GUARDED = {
    "/api/auth/passkeys", "/api/auth/passkeys/{cred_id}",
    "/api/auth/passkeys/{cred_id}/rename",
}

_PARAMS = {"{sub_id}": "1", "{cred_id}": "1", "{txn_uid}": "deadbeef",
           "{income_id}": "1", "{account_id}": "1", "{key}": "nokey",
           "{merchant}": "NOSUCHMERCHANT", "{cat_id}": "nosuchcat"}


def _iter_apiroutes(routes, prefix=""):
    """Recurse through included routers; FastAPI wraps them (_IncludedRouter)
    so child routes never appear directly in app.routes. The wrapper keeps
    the original router; its child paths need the include prefix re-applied
    via include_context when present."""
    for route in routes:
        if isinstance(route, APIRoute):
            if prefix:
                route = _Prefixed(route, prefix)
            yield route
        elif hasattr(route, "original_router"):
            ctx = getattr(route, "include_context", None)
            pfx = getattr(ctx, "prefix", "") if ctx else ""
            yield from _iter_apiroutes(route.original_router.routes, prefix + pfx)
        elif hasattr(route, "routes"):
            yield from _iter_apiroutes(route.routes, prefix)


class _Prefixed:
    """APIRoute view with the include_router prefix applied to .path."""

    def __init__(self, route, prefix):
        self.path = prefix + route.path
        self.methods = route.methods


def test_no_unwalked_wrappers():
    """Every wrapper type in the route table must be one the walker opens."""
    for route in app.routes:
        name = type(route).__name__
        assert name in ("APIRoute", "Route", "Mount") or hasattr(
            route, "original_router") or hasattr(route, "routes"), name


def _walkable_routes():
    for route in _iter_apiroutes(app.routes):
        concrete = route.path
        for pat, val in _PARAMS.items():
            concrete = concrete.replace(pat, val)
        assert "{" not in concrete, f"unmapped path param in {route.path}"
        for method in route.methods - {"HEAD", "OPTIONS"}:
            yield route.path, concrete, method


def test_router_routes_are_walked():
    """The walker must see the included routers, not just app-level routes.
    Guards against FastAPI wrapping include_router children out of sight."""
    patterns = {p for p, _, _ in _walkable_routes()}
    for must in ("/api/transactions", "/api/budgets", "/api/cards",
                 "/api/plaid/sync", "/api/demo/load", "/api/networth",
                 "/api/subscriptions/detect", "/api/categorize/llm"):
        assert must in patterns, f"router route {must} invisible to the walker"


def test_allowlist_matches_expectations():
    """is_public_path must agree exactly with the reviewed allowlist above."""
    for pattern, concrete, _ in _walkable_routes():
        assert is_public_path(concrete) == (pattern in EXPECTED_PUBLIC), pattern


def test_every_route_requires_auth(auth_on, client: TestClient):
    for pattern, concrete, method in _walkable_routes():
        # Empty JSON body so body-reading public endpoints answer 4xx
        # instead of crashing on a missing payload.
        r = client.request(method, concrete, json={})
        if pattern in EXPECTED_PUBLIC and pattern not in SELF_GUARDED:
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
        r = client.request(method, concrete, json={})
        assert r.status_code != 401, f"{method} {concrete} 401 with auth off"


def test_security_headers_present(client: TestClient):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "content-security-policy" in r.headers
    assert r.headers["referrer-policy"] == "no-referrer"
