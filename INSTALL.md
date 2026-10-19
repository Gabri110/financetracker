# FinanceTracker Installation Guide

FinanceTracker is built for one person on their own machine. The defaults (localhost binding, registration closing after the first user) assume that; read **Exposing it to a network** before doing anything else.

## Local

**Requirements:** Python 3.11+

```bash
git clone https://github.com/Gabri110/financetracker.git
cd financetracker
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

The app runs at `http://localhost:8000`. Register your account to get started; registration then closes. The SQLite database is created at `finance.db`, and a random session key at `instance/secret_key`.

## Docker

```bash
docker compose up -d
```

The app listens on `http://localhost:8000` (bound to `127.0.0.1` only). Everything it stores lives on the `finance_data` volume mounted at `/data`:

- `/data/finance.db`: the database
- `/data/secret_key`: the generated session key
- `/data/raw/<bank>/`: bank-statement drop folders

To use a host folder instead of a named volume, map it to `/data` in `docker-compose.yml`.

## Environment Variables

All optional; a `.env` file in the project root is loaded automatically.

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | generated | Session signing key. When unset, a random key is created once and stored in `SECRET_KEY_FILE`. |
| `SECRET_KEY_FILE` | `instance/secret_key` | Where the generated key is kept (`/data/secret_key` in Docker). |
| `DATABASE` | `finance.db` | Path to the SQLite database file. |
| `DATA_DIR` | `data` | Folder holding the `raw/<bank>/` statement drop folders. |
| `REGISTRATION_ENABLED` | `false` | `true` keeps registration open after the first user exists. |
| `FLASK_DEBUG` | off | `1` enables the Flask debugger when running `python app.py`. Never on a reachable host. |

## Exposing it to a network

The app protects a single local user, not several users from each other: bank-statement folders and exchange rates are shared, and form IDs are not ownership-checked. If you still want to reach it from other devices:

1. Keep registration closed (`REGISTRATION_ENABLED` unset).
2. Run it with gunicorn behind a TLS reverse proxy and keep the app port bound to `127.0.0.1`.

```bash
gunicorn -w 2 -b 127.0.0.1:8000 app:app
```

```nginx
server {
    listen 443 ssl;
    server_name finance.example.com;
    # ssl_certificate ... (e.g. via certbot --nginx)

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

The proxy must pass the original `Host` header: the CSRF check compares it with the browser's `Origin`.

## Backup

The whole application state lives in the SQLite database. Either:

- download it from **Options → Download database backup**, or
- copy it safely while the app runs: `sqlite3 finance.db ".backup finance-backup.db"`

To restore, stop the app and replace `finance.db` with the backup. Schema migrations run automatically on the next start.

## Updating

```bash
git pull
pip install -r requirements.txt
python app.py        # or restart your service / `docker compose up -d --build`
```

Database migrations (tracked with `PRAGMA user_version`) run on first request after an update; back up `finance.db` first.
