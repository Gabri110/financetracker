# CLAUDE.md

Guidance for working in this repo. Personal-use project (may be published to GitHub).

## What this is

**FinanceTracker** — a personal finance app (accounts, transactions, budgets, reports,
multi-currency, investments, bank-statement importers). It exists in two largely independent
codebases:

1. **Flask web app** (the primary one) — a single big `app.py` + Jinja `templates/`, backed
   by SQLite. This is what you're almost always working on.
2. **Desktop/mobile app** (`desktop/`) — a *separate* Tauri (Rust) + React/Vite rewrite that
   talks to SQLite directly via Tauri commands. It does **not** import or call the Flask app.
   Changing one does not change the other; they only share the data model conceptually.

The former SaaS mode (Stripe, multi-tenant) and email-receipt ingestion were removed; old
databases may still contain unused `email_inboxes` / `email_transactions` tables.

## Running the web app

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows; use source .venv/bin/activate on *nix
pip install -r requirements-dev.txt
python app.py      # http://localhost:8000 (FLASK_DEBUG=1 for the debugger)
pytest             # test suite
```

The SQLite DB is auto-created on first request via `init_db()`, which then runs the schema
migrations. Default path is `finance.db` in the repo root (override with `DATABASE`). Register
a user in the UI to get started; registration closes once a user exists
(`REGISTRATION_ENABLED=true` keeps it open). `.env` is loaded via python-dotenv.

Docker: `docker compose up -d` → `http://localhost:8000`; data (DB, secret key, statement
folders) on the `/data` volume.

## Architecture & conventions (Flask app)

- **Everything lives in `app.py`** (~6200 lines): config, the SQL schema in `init_db()`,
  migrations, all helpers, the bank parsers and all `@app.route` handlers. There is no
  blueprint/package split — when adding a feature, follow the existing flat style (route +
  helper functions in the same file).
- **DB access:** raw `sqlite3` via `get_db()` (per-request connection on Flask `g`,
  `row_factory = sqlite3.Row`). No ORM. Write SQL directly; always scope queries by
  `user_id = current_user.id` for user data.
- **Auth:** `flask-login`; routes are gated with `@login_required`. Passwords hashed with
  `pbkdf2:sha256` (`hash_password`). CSRF protection is the `reject_cross_site_writes`
  Origin/Referer check plus `SameSite=Lax` cookies — no tokens needed in forms.
- **Templates:** Jinja in `templates/`, Bootstrap-based, charts via Chart.js (CDN). Values
  placed inside `<script>` must go through `|tojson`. Shared fragments live in
  `templates/_macros.html`. Jinja globals: `format_currency`, `user_base_currency()`,
  `supported_currencies`, `registration_open()`.
- **Schema changes:** append a function to `MIGRATIONS` in `app.py` (run once each, tracked
  by `PRAGMA user_version`); never edit or reorder existing ones. New tables can also go in
  the `CREATE TABLE IF NOT EXISTS` script in `init_db()`. Never delete `finance.db` to
  "rebuild" — it is real data.
- **Form numbers:** use `form_amount()` (raises `FormInputError`, shown as a flash message)
  rather than bare `float(request.form[...])`.

### Currency / FX
- Per-account native currency; aggregates convert to a user **base currency** (default `EUR`).
- Rates synced from the **ECB** daily XML (`ECB_RATES_URL`) once a day by
  `ensure_daily_rates` (committed; retried at most hourly when offline), stored in
  `exchange_rates`, and loaded once per request into `g.fx_rates`.
- `get_conversion_rate` uses direct, inverse, then cross rates through the base currency. A
  still-missing rate converts 1:1 *and* is recorded in `g.missing_rates`, which `base.html`
  shows as a warning.
- Use `Decimal` for money/quantity math (see the `DECIMAL_*` constants), not float.

### Imports
- Bank parsers (`parse_revolut_csv`, `parse_trade_republic_csv`, `parse_b100_csv`,
  `parse_edenred_xlsx`, `parse_iberia_cards_xlsx`, `parse_mintos_xlsx`) read files from
  `DATA_DIR/raw/<bank>/`.
- Every importer writes through `upsert_transaction_by_external_id`: re-imports refresh bank
  data but keep the row's existing category and transaction type. Every transaction has an
  `id_transaction` (manual rows get `manual_<random>` from a trigger).
- Category matching for imports: `match_import_category` (rules via `load_category_rules` +
  `auto_categorize_transaction`, then a loose name match).

### Investments
- Tables: `investment_holdings`, `investment_operations`, `investment_snapshots`,
  `investment_valuations`, `market_symbol_cache`. Routes under `/investments/...`.
- Asset classes in `INVESTMENT_ASSET_CLASSES`; live prices pulled from **Stooq** and
  **Yahoo Finance** chart/search endpoints (cached in-memory + DB).
- `VALUATION_TRACKED_CLASSES` (Mintos, Fixed Income Fund) move through evolution valuations:
  the return lives in the value itself â€” never also write a 'dividend' operation for it.
  Evolution baselines come from `get_holding_base_value` (last valuation + contributions
  since). Trades only overwrite `current_price` via `trade_price_or_current`.
- See `ROADMAP.md` for known gaps (transfers, refunds, TR sells/dividends, Mintos withdrawals).

### Bank import & Sync page
- File importers: `BANK_IMPORT_SOURCES` + `BANK_IMPORT_PARSERS` in `app.py`; files live in
  `DATA_DIR/raw/<source>/` (gitignored), dedup by `id_transaction`. Sources marked `archived`
  (B100, Iberia Cards, MyInvestor) are no longer used but kept to re-import historic exports.
- `/sync` (`sync_connectors.py`, the only module besides `app.py`): Revolut (pasted cURL of the
  web app's API), Trade Republic (`pytr`), Bankinter/ING (Enable Banking PSD2). Connectors
  write a CSV in the parser's format into the source folder, then `run_bank_import` runs
  with the settings saved by the last manual import. State lives in user settings:
  `sync.<source>`, `enable_banking.<source>`, `revolut.custom_categories`. See `INSTALL.md`.

## Data & secrets

- **`finance.db` is real personal financial data**, and so are `data/raw/*`, `data.zip` and any
  `*.db` backup. Do not commit, delete, or paste their contents into anything external. All
  are gitignored; tests use synthetic fixtures only.
- The session key is generated into `instance/secret_key` (gitignored) unless `SECRET_KEY` is set.
- Backups: `/export/database` (complete SQLite snapshot, single-user only) or the
  spreadsheet export `/export/full.zip` / `.xlsx` (accounts, categories, rules,
  transactions), restored via `/import/full`.

## Security model

Single user on localhost. Deliberately **not** implemented (revisit before exposing the app):
per-user import folders (`data/raw` is shared), ownership checks on every form ID, per-user
exchange rates, regex-rule ReDoS limits, and spreadsheet formula escaping in exports.

## Notes

- Tests: `pytest` (`tests/`); `tests/conftest.py` gives each test a fresh DB, stubs the ECB
  fetch, and points the bank folders at a temp dir.
- This is a single-user-focused project; favor simple, direct solutions over abstraction.
