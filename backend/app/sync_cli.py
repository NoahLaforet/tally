"""Run a Plaid sync from the command line (no HTTP, no login needed).

Run from backend/:  uv run python -m app.sync_cli

This is what the launchd/systemd daily job calls. It talks to the database
directly, so it works with the passkey wall up and without the server
running. If the server IS running, its open dashboards refresh only on their
next load (the SSE hub lives in the server process, not here).
"""

from __future__ import annotations

import sys

from sqlmodel import Session

from .db import engine, init_db
from .plaid_link import run_sync


def main() -> int:
    init_db()
    result = run_sync()
    print(result)
    # Fresh data landed; check whether anything is worth an alert.
    from .alerts import evaluate_alerts
    with Session(engine) as session:
        print(evaluate_alerts(session, deliver=True))
    return 0 if result.get("configured") else 1


if __name__ == "__main__":
    sys.exit(main())
