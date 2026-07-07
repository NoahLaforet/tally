"""Evaluate alerts from the command line, no HTTP and no login needed.

Run from backend/:  uv run python -m app.alerts_cli

This is the lightweight midday tick. The daily sync already evaluates alerts
after pulling fresh transactions; this second pass catches time-based alerts
(the weekly rollup, a pace warning that crossed the line as the month runs on)
without doing a full Plaid sync. Idempotent, so running it often is harmless.
"""

from __future__ import annotations

import sys

from sqlmodel import Session

from .alerts import evaluate_alerts
from .db import engine, init_db


def main() -> int:
    init_db()
    with Session(engine) as session:
        result = evaluate_alerts(session, deliver=True)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
