"""Auth enforcement and security headers.

The auth gate is structural: every route is protected by default when
AUTH_ENABLED=true, and only the paths listed here are reachable without a
session. New endpoints are therefore protected the moment they are added;
nobody has to remember a per-route dependency. The require_user dependency on
individual routes stays as defense in depth.

Both middlewares are pure ASGI so they do not interfere with streaming
responses (the SSE endpoint).
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from .config import settings

# Reachable without a login. The SPA shell itself is public (it renders the
# login overlay); every route that returns data is not.
PUBLIC_EXACT = {"/", "/healthz", "/favicon.ico"}
PUBLIC_PREFIXES = ("/static/", "/api/auth/")


def is_public_path(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


class AuthGateMiddleware:
    """Reject unauthenticated requests to non-public paths with 401 JSON.

    Must be wrapped by SessionMiddleware (i.e. SessionMiddleware added after
    this one) so scope["session"] is populated before we check it.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not settings.AUTH_ENABLED:
            await self.app(scope, receive, send)
            return
        if is_public_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        session = scope.get("session") or {}
        if not session.get("user_id"):
            response = JSONResponse(status_code=401, content={"detail": "login required"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# HSTS is intentionally absent: TLS terminates at the proxy in front of the
# app (e.g. tailscale serve), which is where HSTS belongs. The CSP allows
# inline script/style because the frontend is a single self-contained file,
# and the Plaid CDN origins because Plaid Link loads from there.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.plaid.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self' https://production.plaid.com https://cdn.plaid.com; "
        "frame-src https://cdn.plaid.com; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    ),
}


class SecurityHeadersMiddleware:
    """Attach the security headers to every HTTP response."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
            await send(message)

        await self.app(scope, receive, send_with_headers)
