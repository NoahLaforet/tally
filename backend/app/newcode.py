"""Print a fresh one-time passkey setup code.

Run from backend/:  uv run python -m app.newcode

Use it to register a passkey on another allowed origin (passkeys are
origin-bound) or to recover after deleting one. Codes are single-use and
expire after 30 minutes; only their hash is stored.
"""

from __future__ import annotations

from sqlmodel import Session

from .auth import SETUP_CODE_TTL, issue_setup_code
from .db import engine, init_db


def main() -> None:
    init_db()
    with Session(engine) as s:
        code = issue_setup_code(s)
    minutes = int(SETUP_CODE_TTL.total_seconds() // 60)
    print(f"One-time passkey setup code (valid {minutes} min): {code}")


if __name__ == "__main__":
    main()
