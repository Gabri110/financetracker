# CLAUDE.md

Guidance for working in this repo. Personal-use project (may be published to GitHub).

## What this is

**FinanceTracker** — a personal finance app (accounts, transactions, budgets, reports,
multi-currency, investments, email-receipt ingestion). It exists in two largely independent
codebases:

1. **Flask web app** (the primary one) — a single big `app.py` + Jinja `templates/`, backed
   by SQLite. This is what you're almost always working on.
2. **Desktop/mobile app** (`desktop/`) — a *separate* Tauri (Rust) + React/Vite rewrite that
   talks to SQLite directly via Tauri commands. It does **not** import or call the Flask app.
   Changing one does not change the other; they only share the data model conceptually.

An optional **SaaS mode** (`saas.py`, Stripe, multi-tenant) is bolted on but irrelevant for
personal use — leave it off (`SAAS_MODE=false`, the default).

## Running the web app

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows; use source .venv/bin/activate on *nix
pip install -r requirements.txt
python app.py      # serves on http://localhost:8000  (debug=True)
```

The SQLite DB is auto-created on first request via `init_db()`; no migration step. Default
path is `finance.db` in the repo root (override with `DATABASE`). Register a user in the UI
to get started.

> **Ports:** `python app.py` and gunicorn serve on **8000**. The Docker setup maps host
> **5000** → container 8000 (`docker-compose.yml`), so the docs' `localhost:5000` is correct
> *only* for the Docker path; direct local runs are on 8000.

Production: `gunicorn -w 4 -b 127.0.0.1:8000 app:app` (see `INSTALL.md`, `Dockerfile`).

## Architecture & conventions (Flask app)

- **Everything lives in `app.py`** (~4600 lines): config, the full SQL schema in `init_db()`,
  all helpers, and all `@app.route` handlers. There is no blueprint/package split for the core
  app — when adding a feature, follow the existing flat style (route + helper functions in the
  same file). `email_processor.py` is the one extracted module (receipt-parsing logic).
- **DB access:** raw `sqlite3` via `get_db()` (per-request connection on Flask `g`,
  `row_factory = sqlite3.Row`). No ORM. Write SQL directly; always scope queries by
  `user_id = current_user.id` for user data.
- **Auth:** `flask-login`; routes are gated with `@login_required`. Passwords hashed with
  `pbkdf2:sha256` (`hash_password`).
- **Templates:** Jinja in `templates/`, Bootstrap-based, charts via Chart.js (CDN). The
  `format_currency` helper is registered as a Jinja global.
- **Schema changes:** edit the `CREATE TABLE` statements in `init_db()`. They use
  `CREATE TABLE IF NOT EXISTS`, so adding a column to an existing local DB needs a manual
  `ALTER TABLE` (or delete `finance.db` to rebuild from scratch).

### Currency / FX
- Per-account native currency; aggregates convert to a user **base currency** (default `EUR`).
- Rates synced from the **ECB** daily XML (`ECB_RATES_URL`), cached in `exchange_rates`.
- Key helpers: `get_user_base_currency`, `ensure_daily_rates`, `convert_amount`.
- Use `Decimal` for money/quantity math (see the `DECIMAL_*` constants), not float.

### Investments
- Tables: `investment_holdings`, `investment_operations`, `investment_snapshots`,
  `investment_valuations`, `market_symbol_cache`. Routes under `/investments/...`.
- Asset classes in `INVESTMENT_ASSET_CLASSES`; live prices pulled from **Stooq** and
  **Yahoo Finance** chart/search endpoints (cached in-memory + DB). This is the area under
  active development (branch `feature/investment_tracking`); see `TODO.md` (in Spanish).

### Bank import & Sync page
- File importers: `BANK_IMPORT_SOURCES` + `BANK_IMPORT_PARSERS` in `app.py`; files live in
  `data/raw/<source>/` (gitignored), dedup by `id_transaction`. Sources marked `archived`
  (B100, Iberia Cards, MyInvestor) are no longer used but kept to re-import historic exports.
- `/sync` (`sync_connectors.py`, the second extracted module): Revolut (pasted cURL of the
  web app's API), Trade Republic (`pytr`), Bankinter/ING (Enable Banking PSD2). Connectors
  write a CSV in the parser's format into the source folder, then `run_bank_import` runs
  with the settings saved by the last manual import. State lives in user settings:
  `sync.<source>`, `enable_banking.<source>`, `revolut.custom_categories`. See `INSTALL.md`.

### Email receipt ingestion
- Users get an inbox address; providers (SendGrid/Mailgun/Postmark) POST to
  `/api/email/inbound`. Parsing lives in `email_processor.py`; parsed items are reviewed and
  approved into transactions.

## Data & secrets

- **`finance.db` is real personal financial data.** Do not commit it, delete it, or paste its
  contents into anything external. It's gitignored.
- Copy `.env.example` → `.env` for config. Never commit real `SECRET_KEY` or Stripe keys.
- Import/export: full backup as `/export/full.zip` and `.xlsx`; restore via `/import/full`.

## Notes

- No automated test suite exists. Verify changes by running the app and exercising the route.
- Stray `.DS_Store` files are macOS artifacts — ignore.
- This is a single-user-focused project; favor simple, direct solutions over abstraction.
