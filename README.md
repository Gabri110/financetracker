# FinanceTracker

A simple personal finance application in the spirit of Quicken and Microsoft Money. Built with Python, Flask, and SQLite, for one person running it on their own machine.

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![Flask](https://img.shields.io/badge/flask-3.1+-green.svg)
![License](https://img.shields.io/badge/license-MIT-blue.svg)

## Features

### Core Features
- **Multi-Account Support** - Track checking, savings, credit cards, cash, and investments
- **Transaction Management** - Add, edit, delete, search and page through transactions
- **Bank Imports** - Revolut, Trade Republic, Bankinter, ING, Edenred and Mintos statements (plus archived B100, Iberia Cards and MyInvestor exports), and a generic CSV import
- **Sync page** - One-click fetching for Revolut, Trade Republic and Bankinter/ING (open banking via Enable Banking), imported with your saved mapping
- **Category Management** - Custom categories with icons and colors
- **Reports & Charts** - Visual spending analysis, monthly trends, savings rate
- **Multi-Currency Accounts** - Keep each account in its native currency while still seeing unified totals
- **Investments** - Holdings, operations, market prices (Stooq/Yahoo) and evolution-tracked assets (Mintos, fixed income)

### Currency & Exchange Rates
- **Account Currency Display** - Transaction rows and account-level values are shown in each account's own currency
- **Base Currency Conversion** - Dashboard, budget, and report aggregates are converted into your selected base currency
- **ECB Daily Sync** - Exchange rates are synced once a day from the European Central Bank (ECB)
- **Manual Rate Overrides** - Edit or insert custom FX rates from the UI when needed

### Smart Categorization
- **Auto-Categorization Rules** - Plain-text or regex patterns, matched on description + payee or payee only
- **Keyboard-Driven Clearing** - Quickly categorize transactions using keyboard shortcuts
- **Re-import safe** - Re-importing a statement refreshes bank data but keeps the categories you set

## Quick Start

### Local

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py                   # http://localhost:8000
```

The first account you register becomes the only one: registration closes after that unless you set `REGISTRATION_ENABLED=true`.

### Docker

```bash
docker compose up -d            # http://localhost:8000
```

The database, the generated secret key and the bank-statement drop folders live on the `finance_data` volume (`/data`).

## Configuration

Everything is optional. Copy `.env.example` to `.env` (it is loaded automatically) or set environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `SECRET_KEY` | Session signing key | generated once and stored in `instance/secret_key` |
| `SECRET_KEY_FILE` | Where the generated key is stored | `instance/secret_key` |
| `DATABASE` | SQLite database path | `finance.db` |
| `DATA_DIR` | Folder holding `raw/<bank>/` statement drop folders | `data` |
| `REGISTRATION_ENABLED` | Keep registration open after the first user | `false` |
| `FLASK_DEBUG` | `1` enables the Flask debugger for `python app.py` | off |

## Project Structure

```
financetracker/
├── app.py                 # The whole Flask application (routes, schema, migrations, importers)
├── sync_connectors.py     # Fetchers behind the Sync page (Revolut, Trade Republic, Enable Banking)
├── requirements.txt       # Runtime dependencies
├── requirements-dev.txt   # + pytest, ruff
├── tests/                 # pytest suite (synthetic fixtures only)
├── Dockerfile
├── docker-compose.yml
├── data/raw/<bank>/       # Drop folders for bank statements (gitignored)
├── templates/             # Jinja templates (Bootstrap 5, Chart.js)
└── desktop/               # Separate Tauri desktop app
```

## Usage Guide

### Importing Bank Transactions

- **Bank statements**: drop the export into `data/raw/<bank>/` (or upload it from **Import → Bank**), map currencies to accounts, preview and confirm.
- **Sync**: after one manual import per source, the **Sync** page fetches new data and imports it with the same settings (setup in [`INSTALL.md`](INSTALL.md#sync-connectors-sync)).
- **Generic CSV** (**Import**): columns `ID_TRANSACTION, DATE, AMOUNT, CATEGORY, DESCRIPTION`; with a header row you may also add `PAYEE` and `TRANSACTION_TYPE`, in any order.
  - Dates `YYYY-MM-DD` or `DD/MM/YYYY`; amounts `1,234.56` or `1.234,56`; comma, semicolon or tab delimited; UTF-8 or Windows-1252.
  - Rows are matched by `ID_TRANSACTION` (or by their content when there is none), so re-importing a file updates rather than duplicates.

### Backups

- **Complete backup**: **Options → Download database backup** saves the whole SQLite database (investments, budgets, settings). Restore it by stopping the app and replacing `finance.db`.
- **Spreadsheet export**: **Import** page → ZIP (CSV files) or Excel with accounts, categories, rules and transactions. These can be restored with **Restore Full Backup** (merge/upsert).

### Keyboard Shortcuts (Clearing Page)

| Key | Action |
|-----|--------|
| `j` / `↓` | Next transaction |
| `k` / `↑` | Previous transaction |
| `1`-`9` | Select category |
| `Enter` | Apply category |
| `r` | Create rule |
| `s` | Skip |

### Managing Currencies

1. Go to **Options** in the sidebar.
2. Select your **Base Currency for Aggregates** and save.
3. Rates sync from the ECB automatically once a day; **Sync ECB Rates Now** forces a refresh.
4. Use **Add / Override Rate Manually** to create or update a rate pair.

If a rate is missing, amounts in that currency are shown unconverted and a warning names the missing pair.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

Schema changes go in the `MIGRATIONS` list in `app.py` (tracked with `PRAGMA user_version`).

## Desktop App (Tauri)

FinanceTracker also ships as a **native desktop application** for Windows, macOS, and Linux. The desktop version:

- Works offline (it only goes online to fetch the daily ECB rates)
- Uses a local SQLite file you choose (no login/password)
- Supports **English**, **Portuguese (BR)**, and **Spanish**
- Includes a **bundled demo database** so you can try it instantly

See [`desktop/README.md`](desktop/README.md) for build instructions.

## Security Notes

The web app is designed for a single user on `localhost`. It has a CSRF origin check, a generated secret key and closed registration, but it does **not** isolate multiple users from each other (shared import folders, global exchange rates). Put it behind a TLS reverse proxy and review [`INSTALL.md`](INSTALL.md) before exposing it to a network.

## Tech Stack

### Web Version
- **Backend**: Python 3.11+, Flask 3.1+
- **Database**: SQLite
- **Frontend**: Bootstrap 5, Chart.js

### Desktop Version
- **Shell**: Tauri v2 (Rust)
- **Backend**: Rust + rusqlite (bundled SQLite)
- **Frontend**: React 18, Vite, Bootstrap 5, Chart.js
- **i18n**: react-i18next (EN, PT-BR, ES)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
