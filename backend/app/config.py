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


class Settings(BaseSettings):
    """Runtime configuration for the Tally backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
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
    # access, and set a real SESSION_SECRET plus RP_ID/ORIGIN for your host.
    AUTH_ENABLED: bool = False
    SESSION_SECRET: str = "dev-insecure-change-me"
    RP_ID: str = "localhost"
    RP_NAME: str = "Tally"
    ORIGIN: str = "http://127.0.0.1:8787"

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
