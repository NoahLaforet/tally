#!/usr/bin/env bash
# Start Tally locally. Seeds the database from real statements on first run,
# then serves the dashboard + API at http://127.0.0.1:8787 (localhost only).
set -e
cd "$(dirname "$0")/backend"

if [ ! -f data/tally.db ]; then
  echo "First run: seeding database from statements..."
  uv run python -m app.seed
fi

echo "Tally running at http://127.0.0.1:8787  (Ctrl-C to stop)"
exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
