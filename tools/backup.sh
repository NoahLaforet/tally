#!/usr/bin/env bash
# Back up the Tally database. VACUUM INTO is the only safe way to copy a WAL
# database while the server is running; a plain cp can capture a torn state.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TALLY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DB="$TALLY_DIR/backend/data/tally.db"
BACKUP_DIR="$TALLY_DIR/backend/data/backups"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "error: sqlite3 not found on PATH." >&2
  exit 1
fi

if [ ! -f "$DB" ]; then
  echo "error: no database at $DB" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/tally-$(date +%Y%m%d-%H%M%S).db"
sqlite3 "$DB" "VACUUM INTO '$OUT'"
echo "Backup written: $OUT"
