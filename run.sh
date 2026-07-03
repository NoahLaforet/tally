#!/usr/bin/env bash
# Start Tally locally. First run creates an empty ledger (set TALLY_SEED_*
# in .env to bulk-import a statements folder), then serves the dashboard +
# API on localhost only. Open http://localhost:8787 (not 127.0.0.1: passkeys
# are origin-bound and cannot live on an IP origin).
set -e
cd "$(dirname "$0")/backend"

if [ ! -f data/tally.db ] || [ "${TALLY_FORCE_SEED:-}" = "1" ]; then
  echo "Seeding database..."
  uv run python -m app.seed
fi

echo "Tally running at http://localhost:8787  (Ctrl-C to stop)"
exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
