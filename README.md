# Tally

A private, local-first personal finance app you run yourself. Tally is a self-hosted alternative to tools like Rocket Money and Copilot Money: it pulls your money into one place, reconciles every statement to the penny, and tells you where you are actually spending, what you are wasting, and which card to put each purchase on. Your data stays on your machine.

Money is tracked as signed integer cents end to end, so balances and totals are exact. No rounding drift, no floating point surprises.

## Why Tally

Most finance apps ask you to upload your accounts to someone else's cloud and trust that the numbers are right. Tally inverts that. It runs on localhost, keeps every byte of real data on disk, and refuses to import a statement unless the parsed transactions add up to the totals printed on the statement. If the math does not tie out to the cent, the import is rejected rather than silently storing a wrong number.

## Features

- **Penny-reconciled statement ingestion.** Import credit card CSV exports, bank PDF statements, and OFX files. Every statement passes a reconciliation gate before any row is written: parsed transaction totals must match the statement's printed totals exactly.
- **Rewards-routing engine.** Knows your cards and their category multipliers, and tells you which card earns the most on a given purchase or merchant category so you stop leaving points on the table.
- **Subscription detector and optimizer.** Automatically finds recurring charges across your accounts, flags price creep and forgotten trials, and surfaces what to cancel or renegotiate.
- **Budgets, income, savings, and net worth.** Set category budgets, track income, watch savings build, and see net worth trend over time across every account.
- **Liquid-glass dashboard.** A fast, single-page interface with dark and light themes and a one-tap privacy mask that blurs every figure on screen when someone is looking over your shoulder.
- **Plaid live-sync.** Optionally connect banks through Plaid for automatic transaction sync, on top of (or instead of) manual statement uploads.
- **Screenshot OCR.** Drop in a screenshot of a card's transaction list and Tally extracts the transactions for you when a clean export is not available.
- **Passkey auth.** Optional WebAuthn passkey login (Touch ID, security keys) for when you expose Tally beyond your own machine.

## Stack

- **Backend:** Python + FastAPI on SQLite, with SQLModel for the data layer.
- **Frontend:** a single self-contained `index.html` of vanilla JavaScript, served directly by FastAPI. No build step, no framework, no node_modules.
- **Integrations:** Plaid for live bank sync, WebAuthn for passkey auth.

## Quick start

Requirements:

- [uv](https://docs.astral.sh/uv/) for the Python environment
- `pdftotext` (from Poppler) for parsing PDF statements

```bash
git clone <your-fork-url> tally
cd tally
./run.sh
```

`run.sh` seeds the database on first run, then serves the dashboard and API at `http://127.0.0.1:8787`, bound to localhost only. Open that URL in your browser.

On macOS you can install the PDF dependency with `brew install poppler`; on Debian or Ubuntu use `apt install poppler-utils`. Backend dependencies are pinned in `backend/pyproject.toml` and resolved by uv on first run.

## Configuration

All configuration lives in a `.env` file in the project root. Copy the template and fill in only what you use:

```bash
cp .env.example .env
```

Every value has a safe local default, so an empty `.env` still runs. See [`.env.example`](.env.example) for the full list, including:

- Passkey auth (off by default for a single local user)
- Plaid keys for live bank sync (leave blank to stay upload-only)
- An optional merchant categorization helper
- First-run seeding paths for your statement folder

The `.env` file is gitignored and never committed.

## Adding a statement

Either drop a statement into your configured statements folder and restart, or POST it to the running server:

```bash
curl -F file=@/path/to/statement.pdf http://127.0.0.1:8787/api/ingest
```

The same pipeline runs, and the open dashboard refreshes itself over Server-Sent Events.

## Privacy model

Tally is local-first by design:

- The server binds to `127.0.0.1` only. Nothing is exposed to your network or the internet unless you choose to expose it.
- All real financial data, the SQLite database, and your `.env` are gitignored. Cloning the repo gives you the code, never anyone's transactions.
- No analytics, no telemetry, no third-party calls except the integrations you explicitly enable (such as Plaid).
- For remote access, put Tally behind [Tailscale](https://tailscale.com/) and enable passkey auth rather than opening a port to the public internet.

## How it works

Three mechanisms keep the data trustworthy:

- **The reconcile gate.** Each statement parser emits a normalized list of transactions. Before any row is written, the pipeline checks that the parsed totals match the totals printed on the statement to the penny. A statement that does not reconcile is rejected, so a parsing error can never quietly corrupt your books.
- **Deterministic `txn_uid` idempotency.** Every transaction gets a deterministic id: a SHA-256 hash of its account, posted date, signed amount in cents, and normalized merchant. Re-importing the same statement produces the same ids and changes nothing, which makes ingestion safe to repeat. A per-statement sequence number disambiguates genuine same-day, same-amount duplicates so they are not collapsed into one.
- **Linked transfers.** Movements between your own accounts are detected and linked into transfer groups via a shared `transfer_group_id`. A card payment appears in checking and on the card; both rows are kept and netted out of spending rather than double-counted.

## Screenshots

See [`docs/`](docs/) for dashboard screenshots and a walkthrough. _(Placeholder: add images to `docs/` and link them here.)_

## License

MIT. See [LICENSE](LICENSE).
