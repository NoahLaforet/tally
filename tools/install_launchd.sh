#!/usr/bin/env bash
# Install the Tally launchd jobs on macOS: the always-on server and the daily sync.
# Renders the templates in deploy/ with this checkout's path and the local uv binary,
# then loads both into the current user's LaunchAgents.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TALLY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

UV_PATH="$(command -v uv || true)"
if [ -z "$UV_PATH" ]; then
  echo "error: uv not found on PATH. Install it from https://docs.astral.sh/uv/ first." >&2
  exit 1
fi

LOG_DIR="$TALLY_DIR/backend/data/logs"
mkdir -p "$LOG_DIR"

AGENTS_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENTS_DIR"

render() {
  # render <template> <destination>
  sed -e "s|__TALLY_DIR__|$TALLY_DIR|g" -e "s|__UV_PATH__|$UV_PATH|g" \
    "$1" > "$2"
  plutil -lint "$2"
}

SERVER_PLIST="$AGENTS_DIR/com.tally.server.plist"
SYNC_PLIST="$AGENTS_DIR/com.tally.sync.plist"

render "$TALLY_DIR/deploy/tally-server.plist.template" "$SERVER_PLIST"
render "$TALLY_DIR/deploy/tally-sync.plist.template" "$SYNC_PLIST"

# Unload first so re-running the installer picks up changes. Tolerate not-loaded.
launchctl unload "$SERVER_PLIST" 2>/dev/null || true
launchctl unload "$SYNC_PLIST" 2>/dev/null || true
launchctl load "$SERVER_PLIST"
launchctl load "$SYNC_PLIST"

echo "Installed:"
echo "  $SERVER_PLIST (server, keeps running, starts at login)"
echo "  $SYNC_PLIST (daily Plaid sync at 08:00)"
echo
echo "Status:"
launchctl list | grep com.tally || echo "  (jobs not visible yet, check 'launchctl list' in a moment)"
echo
echo "Logs: $LOG_DIR/server.log and $LOG_DIR/sync.log"
