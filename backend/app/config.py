"""Application settings.

All configuration is read from the environment with sensible local-first
defaults. Paths are always resolved to absolute paths so the app behaves the
same no matter which directory it is launched from.

Secrets (CLAUDE_API_KEY, PLAID_*) are read from the environment only. They are
never written to disk by this module and never have a default value baked in.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo layout anchor: this file lives at backend/app/config.py, so two parents
# up is the backend/ directory.
BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    """Runtime configuration for the Tally backend."""

    # The documented .env location is the repo root. backend/.env is also read
    # (and wins) for older setups. Absolute paths, so launch directory and
    # process manager (launchd/systemd) do not matter.
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Filesystem. Defaults live under backend/data which is gitignored.
    DATA_DIR: Path = BACKEND_DIR / "data"
    DB_PATH: Path = BACKEND_DIR / "data" / "tally.db"

    # Categorization. The LLM categorizer is opt-in and off by default so the
    # app runs with zero external calls and zero cost out of the box.
    USE_LLM_CATEGORIZER: bool = False

    # Secrets. No defaults. Read from the environment when present.
    CLAUDE_API_KEY: str | None = None
    PLAID_CLIENT_ID: str | None = None
    PLAID_SECRET: str | None = None
    PLAID_ENV: str = "sandbox"
    # OAuth banks (Chase, Wells Fargo) require an https redirect URI that is
    # registered in the Plaid dashboard. Set this to your Tailscale https URL.
    PLAID_REDIRECT_URI: str = ""

    # Server.
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Auth (passkeys / WebAuthn). Off by default so single-user local dev runs
    # without a login wall. Turn on (AUTH_ENABLED=true) for multi-user or remote
    # access, and set a real SESSION_SECRET plus ORIGIN for your host.
    AUTH_ENABLED: bool = False
    SESSION_SECRET: str = "dev-insecure-change-me"
    RP_ID: str = "localhost"
    RP_NAME: str = "Tally"
    ORIGIN: str = "http://127.0.0.1:8787"
    # Comma-separated additional origins the app is reachable on (e.g. a
    # Tailscale https hostname). Passkeys are origin-bound, so each origin a
    # user logs in from needs its own registered passkey.
    EXTRA_ORIGINS: str = ""
    # Set true when the app is served exclusively over HTTPS so the session
    # cookie carries the Secure flag. Left false by default because the
    # primary local origin is plain http://127.0.0.1.
    COOKIE_SECURE: bool = False
    # Absolute session lifetime. 14 days balances convenience for a personal
    # dashboard against how long a stolen cookie stays valid.
    SESSION_MAX_AGE_DAYS: int = 14

    # Uploads. Statements and screenshots are small; anything bigger than this
    # is rejected before it can exhaust memory.
    MAX_UPLOAD_MB: int = 30

    # Encrypts Plaid access tokens at rest (Fernet). Generated and written to
    # the .env automatically the first time a token is stored.
    TALLY_ENCRYPTION_KEY: str | None = None

    @property
    def allowed_origins(self) -> list[str]:
        """Every origin the app may be served from, for WebAuthn verification."""
        out: list[str] = []
        for o in ("http://127.0.0.1:8787", "http://localhost:8787",
                  self.ORIGIN, *self.EXTRA_ORIGINS.split(",")):
            o = o.strip().rstrip("/")
            if o and o not in out:
                out.append(o)
        return out

    @property
    def allowed_rp_ids(self) -> list[str]:
        """Hostnames of the allowed origins that are valid WebAuthn RP IDs.

        IP literals are excluded: the WebAuthn spec forbids them as RP IDs, so
        passkeys work on http://localhost:8787 but can never work on
        http://127.0.0.1:8787. The frontend redirects to localhost when auth
        is on.
        """
        import ipaddress
        from urllib.parse import urlsplit

        out: list[str] = []
        for o in self.allowed_origins:
            host = urlsplit(o).hostname
            if not host or host in out:
                continue
            try:
                ipaddress.ip_address(host)
                continue  # IP literal: not a legal RP ID
            except ValueError:
                out.append(host)
        return out

    @field_validator("DATA_DIR", "DB_PATH")
    @classmethod
    def _resolve_absolute(cls, value: Path) -> Path:
        """Force every path setting to an absolute path."""
        value = Path(value)
        if not value.is_absolute():
            value = (BACKEND_DIR / value).resolve()
        return value.resolve()

    @property
    def statements_dir(self) -> Path:
        """Directory where uploaded source statements are stored."""
        return self.DATA_DIR / "statements"

    def ensure_dirs(self) -> None:
        """Create the data directories if they do not yet exist."""
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.statements_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
