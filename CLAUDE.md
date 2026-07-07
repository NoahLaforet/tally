# Tally

Local-first personal finance app (self-hosted Rocket Money). Bank/card accounts
land in one SQLite ledger; every statement is penny-reconciled against its printed
totals before anything is written. Public repo (MIT); real data never leaves disk,
server binds 127.0.0.1 only.

## Money rules (load-bearing)
- All amounts are signed integer CENTS end to end. Never floats, never strings.
- Transaction UIDs are deterministic hashes (account+date+amount+normalized
  merchant): re-importing a statement must stay a no-op.
- Reconciliation failure = ReconcileError -> 422 + quarantine, typed JSON error
  bodies. Never write unreconciled rows silently (OCR rows are the one exception:
  stored origin='ocr', reconciled=False, behind a preview+confirm ceremony).

## Auth model
- Router-level auth gate: NEW ROUTES ARE PROTECTED BY DEFAULT. If a route must be
  public, add it to the allowlist that test_auth_gate.py walks; keep require_user
  on routes as defense-in-depth.
- Passkeys (webauthn): origin-bound. Use localhost, never 127.0.0.1 (IP literals
  are illegal WebAuthn RP IDs). First passkey needs the one-time console code
  (`uv run python -m app.newcode`).

## Commands
- Run: `./run.sh` (serves http://localhost:8787; config.py's PORT=8000 is unused)
- Test: `cd backend && uv run pytest`
- Daily sync (no HTTP): `uv run python -m app.sync_cli`
- Pre-push: `python3 tools/privacy_scan.py`; backup: `tools/backup.sh`
- No linter/formatter is configured. Don't add one unprompted.

## Layout
backend/app/ (FastAPI, entry app.main:app, routers per domain), backend/app/ingest/
(apple_csv, ofx, wf_pdf, pipeline, convergence), backend/ocr/ (macOS Vision Swift
helper; compiled binary gitignored, build once), frontend/index.html (ONE ~1900-line
vanilla-JS file, no build step), deploy/, tools/. backend/data/ is generated (db,
statements, quarantine, logs); /budget-data.js and /budget-subs.js are generated
per-request from the live DB.

## Gotchas
- PLAID_ENV is load-bearing: sync REFUSES to run in sandbox mode; unknown values
  raise. CSP connect-src hardcodes https://production.plaid.com.
- Apple Card is NOT Plaid: CSV import + OCR only.
- TALLY_ENCRYPTION_KEY is auto-written into .env on first Plaid token store
  (Fernet-encrypts tokens at rest). Losing it orphans stored tokens.
- AUTH_ENABLED=true refuses to boot with the default SESSION_SECRET.
- TLS terminates at `tailscale serve`, not in the app: remote access needs
  EXTRA_ORIGINS + PLAID_REDIRECT_URI on the ts.net host, COOKIE_SECURE=true, and
  a passkey registered per origin.
- OCR on Linux: `uv sync --extra ocr` + tesseract. PDF parsing needs poppler.
- *.local.md planning docs and backend/.env are untracked by design; leave them
  out of git.

## Public repo hygiene
Personal merchant phrasing lives in DB settings (lexicons/savings_options), never
in code. tools/privacy_scan.py runs pre-push and blocks /Users/... paths, *.ts.net
hostnames, and secret shapes. Keep it that way.
