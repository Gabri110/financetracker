import os
import csv
import io
import json
import re
import secrets
import sqlite3
import hashlib
import zipfile
import importlib
from decimal import Decimal, InvalidOperation
from collections import defaultdict
from datetime import datetime
from urllib.error import HTTPError
from urllib import request as urllib_request
from urllib.parse import quote_plus, urlsplit
import xml.etree.ElementTree as ET

from flask import Flask, render_template, request, redirect, url_for, flash, g, jsonify, send_file, abort
from flask import has_app_context
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv is optional; plain env vars still work
    pass

# Use pbkdf2 method since scrypt may not be available on all systems
def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256')

app = Flask(__name__)


def load_secret_key():
    """Use SECRET_KEY from the environment, else a random key persisted in
    SECRET_KEY_FILE (default instance/secret_key)."""
    key = os.environ.get('SECRET_KEY', '').strip()
    if key:
        return key
    path = os.environ.get('SECRET_KEY_FILE') or os.path.join(app.instance_path, 'secret_key')
    try:
        with open(path, encoding='utf-8') as handle:
            key = handle.read().strip()
    except FileNotFoundError:
        key = ''
    if not key:
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        key = secrets.token_hex(32)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(key)
    return key


app.config['SECRET_KEY'] = load_secret_key()
app.config['DATABASE'] = os.environ.get('DATABASE', 'finance.db')
# Registration is open until the first user exists; REGISTRATION_ENABLED=true keeps it open.
app.config['REGISTRATION_ENABLED'] = os.environ.get('REGISTRATION_ENABLED', '').lower() == 'true'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
BACKUP_MAX_MEMBERS = 500
BACKUP_MAX_MEMBER_BYTES = 50 * 1024 * 1024
CATEGORY_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
DEFAULT_CATEGORY_COLOR = '#6c757d'


def normalize_category_color(value):
    """Category colors end up in style attributes and chart JS; only allow #rrggbb."""
    value = (value or '').strip()
    return value if CATEGORY_COLOR_RE.match(value) else DEFAULT_CATEGORY_COLOR

DEFAULT_BASE_CURRENCY = 'EUR'
# Bank-statement drop folders live in <DATA_DIR>/raw/<bank>/ (Docker: on the /data volume).
DATA_DIR = os.environ.get('DATA_DIR', 'data')
RAW_DATA_DIR = os.path.join(DATA_DIR, 'raw')
ECB_RATES_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
SUPPORTED_CURRENCIES = {'EUR', 'USD', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'SEK', 'NOK', 'DKK', 'MXN'}
INVESTMENT_ASSET_CLASSES = (
    'ETF',
    'IF',
    'Fixed Income Fund',
    'Private Equity Fund',
    'Mintos Lending',
)
# Classes whose value moves through manual/imported "evolution" valuations rather
# than market prices; their returns are read from investment_valuations.
VALUATION_TRACKED_CLASSES = ('Mintos Lending', 'Fixed Income Fund')
# Note on the duplicate 'dividend' operations older versions wrote per valuation
# (removed by migrate_drop_synthetic_return_ops).
SYNTHETIC_RETURN_NOTE_PREFIX = 'Auto return from valuation'
INVESTMENT_INCOME_PROFILES = ('fixed', 'variable')
INVESTMENT_MARKET_SYNCABLE_CLASSES = ('ETF', 'IF', 'Fixed Income Fund', 'Private Equity Fund')
BANK_IMPORT_SOURCES = {
    'revolut': {
        'label': 'Revolut',
        'folder': os.path.join(RAW_DATA_DIR, 'revolut'),
        'extensions': ('.csv',),
        'multi_currency': True,
        'investments': False,
        # The Iberia Cards bill is debited from Revolut EUR; without this the
        # charges (imported from the statements) would be double-counted.
        'default_transfer_patterns': ['Iberia Cards'],
    },
    'trade_republic': {
        'label': 'Trade Republic',
        'folder': os.path.join(RAW_DATA_DIR, 'trade_republic'),
        'extensions': ('.csv',),
        'multi_currency': False,
        'investments': True,
    },
    'b100': {
        'label': 'B100',
        'folder': os.path.join(RAW_DATA_DIR, 'B100'),
        'extensions': ('.csv',),
        'multi_currency': False,
        'investments': False,
    },
    'edenred_restaurant': {
        'label': 'Edenred Restaurante',
        'folder': os.path.join(RAW_DATA_DIR, 'edenred'),
        'extensions': ('.xlsx',),
        'multi_currency': False,
        'investments': False,
        # Restaurant-card spending is food; mobility-card spending is transport.
        # Auto-applied to expense rows (matched by category name) unless a user
        # rule already categorises the row.
        'default_category': 'Restaurantes',
    },
    'edenred_mobility': {
        'label': 'Edenred Movilidad',
        'folder': os.path.join(RAW_DATA_DIR, 'edenred'),
        'extensions': ('.xlsx',),
        'multi_currency': False,
        'investments': False,
        'default_category': 'Transporte',
    },
    'iberia_cards': {
        'label': 'Iberia Cards',
        'folder': os.path.join(RAW_DATA_DIR, 'iberia_cards'),
        'extensions': ('.xlsx',),
        'multi_currency': False,
        'investments': False,
    },
}
TRADE_REPUBLIC_INCOME_TYPES = {
    'TRANSFER_INBOUND',
    'TRANSFER_INSTANT_INBOUND',
    'INTEREST_PAYMENT',
    'BENEFITS_SAVEBACK',
}
TRADE_REPUBLIC_BUY_TYPES = {'BUY', 'PRIVATE_MARKET_BUY'}
TRADE_REPUBLIC_ASSET_CLASS_MAP = {
    'MUTUAL_FUND': 'IF',
    'PRIVATE_FUND': 'Private Equity Fund',
}
# Mintos statements (data/raw/mintos/*.xlsx) are imported into the single
# 'Mintos Lending' holding: external deposits become cost basis, and the current
# total value (deposits + net earnings) is booked via an evolution valuation.
MINTOS_FOLDER = os.path.join(RAW_DATA_DIR, 'mintos')
MINTOS_EXTENSIONS = ('.xlsx',)
MINTOS_DEPOSIT_TYPES = {'deposits', 'investors card deposits'}
MINTOS_INTERNAL_TYPES = {
    'investment',
    'secondary market transactions (buy)',
    'principal received',
    'principal received from loan repurchase',
}
MINTOS_EARNING_TYPES = {
    'interest received',
    'interest received from loan repurchase',
    'late fees received',
}
MINTOS_COST_TYPES = {'tax withholding', 'mintos core fee'}
STOOQ_QUOTE_URL = 'https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv'
YAHOO_CHART_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d'
YAHOO_CHART_URL_ALT = 'https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d'
YAHOO_SEARCH_URL = 'https://query1.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=10&newsCount=0'
YAHOO_SEARCH_URL_ALT = 'https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=10&newsCount=0'
HTTP_BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'application/json,text/plain,*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
}
MARKET_QUOTE_CACHE_TTL_SECONDS = 15 * 60
YAHOO_SYMBOL_CACHE_TTL_SECONDS = 24 * 60 * 60
DECIMAL_QUANTITY_PLACES = Decimal('0.00000001')
DECIMAL_MONEY_PLACES = Decimal('0.00000001')
FRACTIONAL_TOLERANCE = Decimal('0.00000001')
QUOTE_SUFFIX_CURRENCY_MAP = {
    'US': 'USD',
    'DE': 'EUR',
    'F': 'EUR',
    'PA': 'EUR',
    'AS': 'EUR',
    'BR': 'EUR',
    'MI': 'EUR',
    'L': 'GBP',
    'SW': 'CHF',
    'TO': 'CAD',
    'V': 'CAD',
    'AX': 'AUD',
    'T': 'JPY',
}
SYMBOL_BY_CURRENCY = {
    'EUR': '€',
    'USD': '$',
    'GBP': '£',
    'JPY': '¥',
    'CAD': 'C$',
    'AUD': 'A$',
    'CHF': 'CHF',
    'SEK': 'kr',
    'NOK': 'kr',
    'DKK': 'kr',
    'MXN': 'MX$',
}

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

MARKET_QUOTE_CACHE = {}
YAHOO_SYMBOL_CACHE = {}


def normalize_currency(currency):
    normalized = (currency or '').strip().upper()
    if not normalized:
        return DEFAULT_BASE_CURRENCY
    return normalized


def format_currency(amount, currency):
    normalized = normalize_currency(currency)
    symbol = SYMBOL_BY_CURRENCY.get(normalized, normalized)
    return f"{symbol}{float(amount):,.2f}"


app.jinja_env.globals['format_currency'] = format_currency

# Database helper functions
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(app.config['DATABASE'])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            account_type TEXT NOT NULL,
            balance REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            category_type TEXT NOT NULL,
            icon TEXT DEFAULT '📁',
            color TEXT DEFAULT '#6c757d',
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            category_id INTEGER,
            transaction_type TEXT NOT NULL,
            amount REAL NOT NULL,
            description TEXT,
            payee TEXT,
            date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (account_id) REFERENCES accounts (id),
            FOREIGN KEY (category_id) REFERENCES categories (id)
        );

        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            period TEXT DEFAULT 'monthly',
            start_date DATE,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (category_id) REFERENCES categories (id)
        );

        CREATE TABLE IF NOT EXISTS category_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category_id INTEGER NOT NULL,
            pattern TEXT NOT NULL,
            match_field TEXT DEFAULT 'description',
            is_regex INTEGER DEFAULT 0,
            priority INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (category_id) REFERENCES categories (id)
        );

        CREATE TABLE IF NOT EXISTS exchange_rates (
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            provider TEXT DEFAULT 'ecb',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (from_currency, to_currency)
        );

        CREATE TABLE IF NOT EXISTS investment_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            asset_name TEXT NOT NULL,
            ticker TEXT,
            isin TEXT,
            asset_class TEXT NOT NULL,
            currency TEXT DEFAULT 'EUR',
            quantity REAL NOT NULL DEFAULT 0,
            avg_cost REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (account_id) REFERENCES accounts (id)
        );

        CREATE TABLE IF NOT EXISTS investment_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            account_id INTEGER NOT NULL,
            holding_id INTEGER,
            operation_type TEXT NOT NULL,
            quantity REAL,
            price_per_unit REAL,
            amount REAL NOT NULL,
            currency TEXT DEFAULT 'USD',
            is_reinvested INTEGER DEFAULT 0,
            realized_gain REAL,
            note TEXT,
            operation_date DATE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (account_id) REFERENCES accounts (id),
            FOREIGN KEY (holding_id) REFERENCES investment_holdings (id)
        );

        CREATE TABLE IF NOT EXISTS investment_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            snapshot_date DATE NOT NULL,
            total_value REAL NOT NULL,
            currency TEXT DEFAULT 'USD',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id)
        );

        CREATE TABLE IF NOT EXISTS investment_valuations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            holding_id INTEGER NOT NULL,
            valuation_date DATE NOT NULL,
            total_value REAL NOT NULL,
            currency TEXT DEFAULT 'USD',
            return_pct REAL,
            return_amount REAL,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (holding_id) REFERENCES investment_holdings (id)
        );

        CREATE TABLE IF NOT EXISTS market_symbol_cache (
            identifier TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            source TEXT DEFAULT 'yahoo',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER NOT NULL,
            setting_key TEXT NOT NULL,
            value_json TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, setting_key),
            FOREIGN KEY (user_id) REFERENCES users (id)
        );
    ''')
    db.commit()
    run_migrations(db)


# ---------------------------------------------------------------------------
# Schema migrations. PRAGMA user_version records how many have run; to change
# the schema of existing databases, append a function to MIGRATIONS (never edit
# or reorder the ones already there).
# ---------------------------------------------------------------------------

def add_column_if_missing(db, table, column, definition):
    columns = {row['name'] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}
    if column in columns:
        return
    try:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
    except sqlite3.OperationalError as exc:
        # Another worker may have added it between the check and the ALTER.
        if 'duplicate column' not in str(exc).lower():
            raise


def migrate_add_late_columns(db):
    """Columns added after the first release (previously ad-hoc checks in init_db)."""
    add_column_if_missing(db, 'transactions', 'id_transaction', 'TEXT')
    add_column_if_missing(db, 'users', 'base_currency', f"TEXT DEFAULT '{DEFAULT_BASE_CURRENCY}'")
    add_column_if_missing(db, 'accounts', 'income_profile', 'TEXT')
    add_column_if_missing(db, 'accounts', 'is_investment_dashboard', 'INTEGER DEFAULT 0')
    add_column_if_missing(db, 'investment_holdings', 'isin', 'TEXT')
    # External reference used by bank imports to make operation creation idempotent.
    add_column_if_missing(db, 'investment_operations', 'id_transaction', 'TEXT')


def migrate_add_indexes(db):
    for statement in (
        'CREATE INDEX IF NOT EXISTS idx_transactions_user_ext ON transactions (user_id, id_transaction)',
        'CREATE INDEX IF NOT EXISTS idx_transactions_user_date ON transactions (user_id, date)',
        'CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions (account_id)',
        'CREATE INDEX IF NOT EXISTS idx_investment_operations_holding ON investment_operations (holding_id)',
    ):
        db.execute(statement)


def migrate_drop_synthetic_return_ops(db):
    """Evolution returns live in investment_valuations.return_amount. Older code also
    wrote a matching 'dividend' operation for each one, double-counting the return."""
    db.execute(
        f"""DELETE FROM investment_operations
           WHERE operation_type = 'dividend' AND note LIKE '{SYNTHETIC_RETURN_NOTE_PREFIX}%'"""
    )


def migrate_external_ids_for_manual_rows(db):
    """Give every transaction an external id so backups round-trip: manual rows
    used to export without one, so restoring duplicated them and two identical
    manual entries collapsed into one. A trigger covers rows inserted later."""
    db.execute(
        """UPDATE transactions SET id_transaction = 'manual_' || lower(hex(randomblob(8)))
           WHERE id_transaction IS NULL OR id_transaction = ''"""
    )
    db.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_transactions_external_id
           AFTER INSERT ON transactions
           WHEN NEW.id_transaction IS NULL OR NEW.id_transaction = ''
           BEGIN
               UPDATE transactions SET id_transaction = 'manual_' || lower(hex(randomblob(8)))
               WHERE id = NEW.id;
           END"""
    )


MIGRATIONS = [
    migrate_add_late_columns,
    migrate_add_indexes,
    migrate_drop_synthetic_return_ops,
    migrate_external_ids_for_manual_rows,
]


def run_migrations(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    for number, migration in enumerate(MIGRATIONS, start=1):
        if number <= version:
            continue
        migration(db)
        db.execute(f'PRAGMA user_version = {number}')
        db.commit()


def get_user_base_currency(user_id):
    db = get_db()
    row = db.execute(
        'SELECT COALESCE(base_currency, ?) AS base_currency FROM users WHERE id = ?',
        (DEFAULT_BASE_CURRENCY, user_id),
    ).fetchone()
    if not row:
        return DEFAULT_BASE_CURRENCY
    return normalize_currency(row['base_currency'])


def current_user_base_currency():
    if has_app_context() and current_user and current_user.is_authenticated:
        return get_user_base_currency(current_user.id)
    return DEFAULT_BASE_CURRENCY


app.jinja_env.globals['user_base_currency'] = current_user_base_currency
app.jinja_env.globals['supported_currencies'] = sorted(SUPPORTED_CURRENCIES)


def get_user_setting(db, user_id, key, default=None):
    row = db.execute(
        'SELECT value_json FROM user_settings WHERE user_id = ? AND setting_key = ?',
        (user_id, key),
    ).fetchone()
    if not row:
        return default
    try:
        return json.loads(row['value_json'])
    except (TypeError, ValueError):
        return default


def set_user_setting(db, user_id, key, value):
    db.execute(
        '''INSERT INTO user_settings (user_id, setting_key, value_json)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id, setting_key)
           DO UPDATE SET value_json = excluded.value_json, updated_at = CURRENT_TIMESTAMP''',
        (user_id, key, json.dumps(value)),
    )


def fetch_ecb_rates_for_base(base_currency):
    base_currency = normalize_currency(base_currency)
    with urllib_request.urlopen(ECB_RATES_URL, timeout=8) as response:
        xml_content = response.read()

    root = ET.fromstring(xml_content)
    cube_nodes = root.findall('.//{*}Cube[@currency][@rate]')

    eur_pairs = {'EUR': 1.0}
    for node in cube_nodes:
        code = normalize_currency(node.attrib.get('currency'))
        try:
            eur_pairs[code] = float(node.attrib.get('rate'))
        except (TypeError, ValueError):
            continue

    if base_currency not in eur_pairs:
        raise ValueError(f'Missing ECB rate for base currency {base_currency}')

    base_vs_eur = eur_pairs[base_currency]
    rates = {}
    for from_currency, eur_vs_from in eur_pairs.items():
        if from_currency == base_currency:
            rates[from_currency] = 1.0
        else:
            rates[from_currency] = base_vs_eur / eur_vs_from

    return rates


RATE_SYNC_RETRY_SECONDS = 3600
_last_rate_sync_attempt = {}


def ensure_daily_rates(db, base_currency):
    base_currency = normalize_currency(base_currency)

    # updated_at is CURRENT_TIMESTAMP (UTC), so compare against SQLite's UTC date too.
    has_today_rates = db.execute(
        '''SELECT 1 FROM exchange_rates
           WHERE to_currency = ? AND date(updated_at) = date('now')
           LIMIT 1''',
        (base_currency,),
    ).fetchone()
    if has_today_rates:
        return

    # When offline, don't stall every page on the ECB timeout: retry hourly.
    now = datetime.now().timestamp()
    if now - _last_rate_sync_attempt.get(base_currency, 0) < RATE_SYNC_RETRY_SECONDS:
        return
    _last_rate_sync_attempt[base_currency] = now

    try:
        sync_rates_for_base(db, base_currency)
        db.commit()
    except Exception:
        db.rollback()


def sync_rates_for_base(db, base_currency):
    base_currency = normalize_currency(base_currency)
    rates = fetch_ecb_rates_for_base(base_currency)
    upserted = 0

    for from_currency, rate in rates.items():
        db.execute(
            '''INSERT INTO exchange_rates (from_currency, to_currency, rate, provider, updated_at)
               VALUES (?, ?, ?, 'ecb', CURRENT_TIMESTAMP)
               ON CONFLICT(from_currency, to_currency)
               DO UPDATE SET rate = excluded.rate, provider = excluded.provider, updated_at = CURRENT_TIMESTAMP''',
            (from_currency, base_currency, rate),
        )

        upserted += 1

    invalidate_rate_cache()
    return upserted


def load_rate_table(db):
    """All stored rates as {(from, to): rate}, read once per request."""
    if has_app_context() and 'fx_rates' in g:
        return g.fx_rates
    table = {
        (row['from_currency'], row['to_currency']): float(row['rate'])
        for row in db.execute('SELECT from_currency, to_currency, rate FROM exchange_rates WHERE rate > 0')
    }
    if has_app_context():
        g.fx_rates = table
    return table


def invalidate_rate_cache():
    if has_app_context():
        g.pop('fx_rates', None)


def get_conversion_rate(db, from_currency, to_currency):
    from_currency = normalize_currency(from_currency)
    to_currency = normalize_currency(to_currency)

    if from_currency == to_currency:
        return 1.0

    rates = load_rate_table(db)
    if (from_currency, to_currency) in rates:
        return rates[(from_currency, to_currency)]
    if (to_currency, from_currency) in rates:
        return 1.0 / rates[(to_currency, from_currency)]

    # Rates are stored as X -> base currency; cross two of them through that base.
    for (source, pivot), rate in rates.items():
        if source == from_currency and (to_currency, pivot) in rates:
            return rate / rates[(to_currency, pivot)]

    return None


def convert_amount(db, amount, from_currency, to_currency):
    rate = get_conversion_rate(db, from_currency, to_currency)
    if rate is None:
        # Shown unconverted; base.html warns about it instead of failing silently.
        if has_app_context():
            g.setdefault('missing_rates', set()).add(
                (normalize_currency(from_currency), normalize_currency(to_currency))
            )
        return float(amount)
    return float(amount) * rate


def normalize_ticker_symbol(ticker):
    return (ticker or '').strip().upper().replace(' ', '')


def normalize_isin(isin):
    return (isin or '').strip().upper().replace(' ', '')


def is_isin(value):
    normalized = normalize_isin(value)
    return bool(re.fullmatch(r'[A-Z]{2}[A-Z0-9]{10}', normalized))


def normalize_market_identifier(identifier):
    return (identifier or '').strip().upper().replace(' ', '')


def get_cached_item(cache_store, key, ttl_seconds):
    entry = cache_store.get(key)
    if not entry:
        return None
    if (datetime.now() - entry['timestamp']).total_seconds() > ttl_seconds:
        cache_store.pop(key, None)
        return None
    return entry['value']


def set_cached_item(cache_store, key, value):
    cache_store[key] = {
        'timestamp': datetime.now(),
        'value': value,
    }


def parse_db_timestamp(timestamp_text):
    if not timestamp_text:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.strptime(timestamp_text, fmt)
        except ValueError:
            continue
    return None


def get_persisted_symbol(identifier, ttl_seconds):
    if not has_app_context():
        return None

    db = get_db()
    row = db.execute(
        'SELECT symbol, updated_at FROM market_symbol_cache WHERE identifier = ?',
        (identifier,),
    ).fetchone()
    if not row:
        return None

    updated_at = parse_db_timestamp(row['updated_at'])
    if not updated_at:
        return normalize_ticker_symbol(row['symbol'])

    if (datetime.now() - updated_at).total_seconds() > ttl_seconds:
        db.execute('DELETE FROM market_symbol_cache WHERE identifier = ?', (identifier,))
        db.commit()
        return None

    return normalize_ticker_symbol(row['symbol'])


def persist_symbol(identifier, symbol, source='yahoo'):
    if not has_app_context():
        return

    db = get_db()
    db.execute(
        '''INSERT INTO market_symbol_cache (identifier, symbol, source, updated_at)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(identifier)
           DO UPDATE SET symbol = excluded.symbol,
                         source = excluded.source,
                         updated_at = CURRENT_TIMESTAMP''',
        (identifier, symbol, source),
    )
    db.commit()


def fetch_json_with_headers(url):
    request_obj = urllib_request.Request(url, headers=HTTP_BROWSER_HEADERS)
    with urllib_request.urlopen(request_obj, timeout=8) as response:
        return json.loads(response.read().decode('utf-8'))


def fetch_text_with_headers(url):
    request_obj = urllib_request.Request(url, headers=HTTP_BROWSER_HEADERS)
    with urllib_request.urlopen(request_obj, timeout=8) as response:
        return response.read().decode('utf-8', errors='ignore')


def infer_quote_currency_from_ticker(ticker):
    normalized = normalize_ticker_symbol(ticker)
    if '.' not in normalized:
        return None
    suffix = normalized.split('.')[-1]
    return QUOTE_SUFFIX_CURRENCY_MAP.get(suffix)


def infer_quote_currency_from_stooq_source(source_label):
    if not source_label or '(' not in source_label or ')' not in source_label:
        return None
    symbol = source_label.split('(', 1)[1].split(')', 1)[0]
    return infer_quote_currency_from_ticker(symbol)


def resolve_holding_market_identifiers(holding):
    asset_class = (holding['asset_class'] or '').strip()
    ticker = normalize_ticker_symbol(holding['ticker'])
    isin = normalize_isin(holding['isin'])

    ordered = []
    if asset_class == 'IF':
        ordered = [isin, ticker]
    else:
        ordered = [ticker, isin]

    seen = set()
    identifiers = []
    for candidate in ordered:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        identifiers.append(candidate)
    return identifiers


def fetch_stooq_price(identifier):
    normalized = normalize_market_identifier(identifier)
    if not normalized:
        raise ValueError('Ticker/identifier is required.')

    if is_isin(normalized):
        raise ValueError('Stooq lookup by ISIN is not supported.')

    candidates = [normalized]
    if '.' not in normalized:
        candidates.append(f'{normalized}.US')

    for symbol in candidates:
        try:
            url = STOOQ_QUOTE_URL.format(symbol=symbol.lower())
            csv_text = fetch_text_with_headers(url)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            if not rows:
                continue
            close_value = (rows[0].get('Close') or '').strip()
            if not close_value or close_value == 'N/D':
                continue
            return {
                'price': float(close_value),
                'currency': None,
                'source': f'Stooq ({symbol})',
            }
        except Exception:
            continue

    raise ValueError('Stooq could not provide a valid close price.')


def resolve_yahoo_symbol(identifier):
    normalized = normalize_market_identifier(identifier)
    if not normalized:
        return None

    cached_symbol = get_cached_item(YAHOO_SYMBOL_CACHE, normalized, YAHOO_SYMBOL_CACHE_TTL_SECONDS)
    if cached_symbol:
        return cached_symbol

    persisted_symbol = get_persisted_symbol(normalized, YAHOO_SYMBOL_CACHE_TTL_SECONDS)
    if persisted_symbol:
        set_cached_item(YAHOO_SYMBOL_CACHE, normalized, persisted_symbol)
        return persisted_symbol

    errors = []
    for template in (YAHOO_SEARCH_URL, YAHOO_SEARCH_URL_ALT):
        url = template.format(query=quote_plus(normalized))
        try:
            payload = fetch_json_with_headers(url)
            for quote in (payload.get('quotes') or []):
                symbol = normalize_ticker_symbol(quote.get('symbol'))
                if symbol:
                    set_cached_item(YAHOO_SYMBOL_CACHE, normalized, symbol)
                    persist_symbol(normalized, symbol, source='yahoo')
                    return symbol
        except HTTPError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:
            errors.append(str(exc))
            continue

    if errors:
        raise ValueError('; '.join(errors))
    return None


def fetch_yahoo_price(identifier):
    normalized = normalize_market_identifier(identifier)
    if not normalized:
        raise ValueError('Ticker/identifier is required.')

    query_symbol = normalized
    if is_isin(normalized):
        resolved_symbol = resolve_yahoo_symbol(normalized)
        if not resolved_symbol:
            raise ValueError('Yahoo search could not resolve this ISIN.')
        query_symbol = resolved_symbol

    payload = None
    errors = []
    for template in (YAHOO_CHART_URL, YAHOO_CHART_URL_ALT):
        url = template.format(ticker=quote_plus(query_symbol))
        try:
            payload = fetch_json_with_headers(url)
            break
        except HTTPError as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:
            errors.append(str(exc))
            continue

    if payload is None:
        raise ValueError('; '.join(errors) if errors else 'Yahoo chart request failed.')

    result_list = (((payload or {}).get('chart') or {}).get('result') or [])
    if not result_list:
        raise ValueError('Yahoo returned no chart result.')

    result = result_list[0]
    meta = result.get('meta') or {}
    quote_items = ((result.get('indicators') or {}).get('quote') or [])
    if not quote_items:
        raise ValueError('Yahoo returned no quote payload.')

    closes = quote_items[0].get('close') or []
    close_price = None
    for value in reversed(closes):
        if value is not None:
            close_price = float(value)
            break

    if close_price is None:
        raise ValueError('Yahoo returned no close values.')

    quote_currency = (meta.get('currency') or '').strip()
    # London listings quote in pence ('GBp'/'GBX'); normalizing that to GBP
    # would value the holding 100x too high.
    if quote_currency in ('GBp', 'GBX'):
        close_price /= 100.0
        quote_currency = 'GBP'

    return {
        'price': close_price,
        'currency': normalize_currency(quote_currency),
        'source': f'Yahoo ({query_symbol})',
    }


def fetch_market_price(identifier):
    errors = []
    normalized = normalize_market_identifier(identifier)
    cached_quote = get_cached_item(MARKET_QUOTE_CACHE, normalized, MARKET_QUOTE_CACHE_TTL_SECONDS)
    if cached_quote:
        return cached_quote

    identifier_is_isin = is_isin(normalized)
    stooq_quote = None
    if not identifier_is_isin:
        try:
            stooq_quote = fetch_stooq_price(identifier)
            if not stooq_quote.get('currency'):
                stooq_quote['currency'] = infer_quote_currency_from_ticker(identifier)
            if not stooq_quote.get('currency'):
                stooq_quote['currency'] = infer_quote_currency_from_stooq_source(stooq_quote.get('source'))
            if stooq_quote.get('currency'):
                set_cached_item(MARKET_QUOTE_CACHE, normalized, stooq_quote)
                return stooq_quote
        except Exception as exc:
            errors.append(str(exc))

    try:
        yahoo_quote = fetch_yahoo_price(identifier)
        if stooq_quote and stooq_quote.get('price'):
            # Keep Stooq as preferred price source, enrich with Yahoo currency.
            stooq_quote['currency'] = yahoo_quote.get('currency') or stooq_quote.get('currency')
            set_cached_item(MARKET_QUOTE_CACHE, normalized, stooq_quote)
            return stooq_quote
        set_cached_item(MARKET_QUOTE_CACHE, normalized, yahoo_quote)
        return yahoo_quote
    except Exception as exc:
        errors.append(str(exc))

    # Final fallback if Stooq had a price but currency enrichment failed.
    if stooq_quote and stooq_quote.get('price'):
        return stooq_quote

    if errors and all('429' in str(err) for err in errors):
        raise ValueError('Market data provider rate-limited the request (HTTP 429). Please retry in a minute.')

    raise ValueError('; '.join(errors) if errors else 'No provider returned a market price.')


def convert_quote_to_holding_currency(db, quote_price, quote_currency, holding_currency, base_currency):
    quote_currency = normalize_currency(quote_currency)
    holding_currency = normalize_currency(holding_currency)

    if not quote_currency or quote_currency == holding_currency:
        return float(quote_price)

    if quote_currency not in SUPPORTED_CURRENCIES or holding_currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f'Unsupported quote currency conversion {quote_currency} -> {holding_currency}.')

    ensure_daily_rates(db, base_currency)
    return convert_amount(db, quote_price, quote_currency, holding_currency)


def detect_delimiter(sample_text):
    # Only the header line is trusted: descriptions in data rows may contain ';'.
    header = (sample_text or '').lstrip('﻿').split('\n', 1)[0]
    if '\t' in header:
        return '\t'
    if header.count(';') > header.count(','):
        return ';'
    return ','


def parse_amount_string(raw_amount):
    amount_str = (raw_amount or '').replace('"', '').replace(' ', '').strip()
    if not amount_str:
        raise ValueError('Missing amount')

    if ',' in amount_str and '.' in amount_str:
        # Both present: whichever comes last is the decimal separator.
        last_comma = amount_str.rfind(',')
        last_dot = amount_str.rfind('.')
        if last_comma > last_dot:
            amount_str = amount_str.replace('.', '').replace(',', '.')
        else:
            amount_str = amount_str.replace(',', '')
    else:
        separator = ',' if ',' in amount_str else '.' if '.' in amount_str else None
        if separator:
            integer_part, _, fraction = amount_str.rpartition(separator)
            digits = integer_part.lstrip('+-')
            is_grouping = amount_str.count(separator) > 1 or (
                len(fraction) == 3 and 1 <= len(digits) <= 3 and digits != '0'
            )
            # '1.234' / '1,234' / '1.234.567' are thousands; '12,5' / '0,5' / '0.125' are decimals.
            amount_str = amount_str.replace(separator, '') if is_grouping else amount_str.replace(separator, '.')

    return float(amount_str)


def parse_import_date(raw_date):
    normalized = (raw_date or '').strip()
    if not normalized:
        raise ValueError('Missing date')

    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y', '%m-%d-%Y'):
        try:
            return datetime.strptime(normalized, fmt).strftime('%Y-%m-%d')
        except ValueError:
            continue
    raise ValueError(f"Invalid date format '{raw_date}'")


def normalized_key(*parts):
    return '||'.join((str(part or '').strip().lower() for part in parts))


def stable_transaction_id(account_name, account_currency, date, amount, description, payee):
    base = '|'.join([
        str(account_name or '').strip().lower(),
        str(account_currency or '').strip().upper(),
        str(date or '').strip(),
        f"{float(amount):.2f}",
        str(description or '').strip().lower(),
        str(payee or '').strip().lower(),
    ])
    digest = hashlib.sha256(base.encode('utf-8')).hexdigest()[:16]
    return f'auto_{digest}'


def rows_to_csv_bytes(headers, rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row.get(header, '') for header in headers])
    return output.getvalue().encode('utf-8')


def csv_text_to_dict_rows(csv_text):
    if not csv_text:
        return []
    delimiter = detect_delimiter(csv_text[:2000])
    reader = csv.DictReader(io.StringIO(csv_text), delimiter=delimiter)
    rows = []
    for row in reader:
        normalized = {str(k or '').strip().upper(): (v or '').strip() for k, v in row.items()}
        if any(value for value in normalized.values()):
            rows.append(normalized)
    return rows


def fetch_accounts_export_rows(db, user_id):
    accounts = db.execute(
        '''SELECT id, name, account_type, currency, created_at
           FROM accounts WHERE user_id = ? ORDER BY name''',
        (user_id,),
    ).fetchall()
    rows = []
    for account in accounts:
        current_balance = get_account_current_balance(db, user_id, account['id'])
        rows.append({
            'ACCOUNT_NAME': account['name'],
            'ACCOUNT_TYPE': account['account_type'],
            'CURRENCY': normalize_currency(account['currency']),
            'CURRENT_BALANCE': f"{float(current_balance):.2f}",
            'CREATED_AT': account['created_at'] or '',
        })
    return rows


def fetch_categories_export_rows(db, user_id):
    categories = db.execute(
        '''SELECT name, category_type, icon, color
           FROM categories WHERE user_id = ? ORDER BY category_type, name''',
        (user_id,),
    ).fetchall()
    return [{
        'NAME': row['name'],
        'CATEGORY_TYPE': row['category_type'],
        'ICON': row['icon'] or '',
        'COLOR': row['color'] or '',
    } for row in categories]


def fetch_rules_export_rows(db, user_id):
    rules = db.execute(
        '''SELECT r.pattern, r.match_field, r.is_regex, r.priority,
                  c.name AS category_name, c.category_type AS category_type
           FROM category_rules r
           JOIN categories c ON r.category_id = c.id
           WHERE r.user_id = ?
           ORDER BY r.priority DESC, r.pattern''',
        (user_id,),
    ).fetchall()
    return [{
        'PATTERN': row['pattern'],
        'CATEGORY_NAME': row['category_name'],
        'CATEGORY_TYPE': row['category_type'],
        'MATCH_FIELD': row['match_field'] or 'description',
        'IS_REGEX': '1' if row['is_regex'] else '0',
        'PRIORITY': str(row['priority'] if row['priority'] is not None else 0),
    } for row in rules]


def fetch_transactions_export_rows(db, user_id, filters=None):
    filters = filters or {}
    query = '''
        SELECT t.id_transaction, t.date, t.transaction_type, t.amount, t.description, t.payee,
               a.name AS account_name, a.currency AS account_currency, a.account_type,
               c.name AS category_name
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ?
    '''
    params = [user_id]

    account_id = filters.get('account_id')
    category_id = filters.get('category_id')
    trans_type = filters.get('type')
    start_date = filters.get('start_date')
    end_date = filters.get('end_date')
    search = (filters.get('search') or '').strip()

    if account_id:
        query += ' AND t.account_id = ?'
        params.append(account_id)
    if category_id:
        query += ' AND t.category_id = ?'
        params.append(category_id)
    if trans_type:
        query += ' AND t.transaction_type = ?'
        params.append(trans_type)
    if start_date:
        query += ' AND t.date >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND t.date <= ?'
        params.append(end_date)
    if search:
        query += ' AND (t.description LIKE ? OR t.payee LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%'])

    query += ' ORDER BY t.date DESC, t.id DESC'

    records = db.execute(query, params).fetchall()
    rows = []
    for row in records:
        sign = -1 if row['transaction_type'] == 'expense' else 1
        amount_value = float(row['amount']) * sign
        if row['transaction_type'] == 'transfer':
            amount_value = float(row['amount'])

        rows.append({
            'ID_TRANSACTION': row['id_transaction'] or '',
            'DATE': row['date'],
            'AMOUNT': f"{amount_value:.2f}",
            'CATEGORY': row['category_name'] or '',
            'DESCRIPTION': row['description'] or '',
            'PAYEE': row['payee'] or '',
            'ACCOUNT_NAME': row['account_name'],
            'ACCOUNT_CURRENCY': normalize_currency(row['account_currency']),
            'ACCOUNT_TYPE': row['account_type'],
            'TRANSACTION_TYPE': row['transaction_type'],
        })
    return rows


TRANSACTION_EXPORT_HEADERS = [
    'ID_TRANSACTION', 'DATE', 'AMOUNT', 'CATEGORY', 'DESCRIPTION', 'PAYEE',
    'ACCOUNT_NAME', 'ACCOUNT_CURRENCY', 'ACCOUNT_TYPE', 'TRANSACTION_TYPE',
]


def upsert_accounts_from_rows(db, user_id, account_rows):
    existing = db.execute(
        '''SELECT id, name, account_type, currency
           FROM accounts WHERE user_id = ?''',
        (user_id,),
    ).fetchall()
    cache = {
        normalized_key(row['name'], row['currency'], row['account_type']): row['id']
        for row in existing
    }
    created = 0

    for row in account_rows:
        name = (row.get('ACCOUNT_NAME') or '').strip()
        if not name:
            continue
        account_type = (row.get('ACCOUNT_TYPE') or 'checking').strip().lower()
        currency = normalize_currency(row.get('CURRENCY'))
        key = normalized_key(name, currency, account_type)

        if key not in cache:
            db.execute(
                '''INSERT INTO accounts (user_id, name, account_type, currency, balance)
                   VALUES (?, ?, ?, ?, 0)''',
                (user_id, name, account_type, currency),
            )
            cache[key] = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            created += 1

    return cache, created


def upsert_categories_from_rows(db, user_id, category_rows):
    existing = db.execute(
        'SELECT id, name, category_type FROM categories WHERE user_id = ?',
        (user_id,),
    ).fetchall()
    cache = {normalized_key(row['name'], row['category_type']): row['id'] for row in existing}
    inserted = 0
    updated = 0

    for row in category_rows:
        name = (row.get('NAME') or '').strip()
        category_type = (row.get('CATEGORY_TYPE') or '').strip().lower()
        if not name or category_type not in ('income', 'expense', 'transfer'):
            continue

        icon = (row.get('ICON') or '📁').strip() or '📁'
        color = normalize_category_color(row.get('COLOR'))
        key = normalized_key(name, category_type)
        existing_id = cache.get(key)

        if existing_id:
            db.execute(
                '''UPDATE categories
                   SET icon = ?, color = ?
                   WHERE id = ? AND user_id = ?''',
                (icon, color, existing_id, user_id),
            )
            updated += 1
        else:
            db.execute(
                '''INSERT INTO categories (user_id, name, category_type, icon, color)
                   VALUES (?, ?, ?, ?, ?)''',
                (user_id, name, category_type, icon, color),
            )
            new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
            cache[key] = new_id
            inserted += 1

    return cache, inserted, updated


def upsert_rules_from_rows(db, user_id, rule_rows, category_cache):
    inserted = 0
    updated = 0
    skipped = 0
    errors = []

    for idx, row in enumerate(rule_rows, 1):
        pattern = (row.get('PATTERN') or '').strip()
        category_name = (row.get('CATEGORY_NAME') or '').strip()
        category_type = (row.get('CATEGORY_TYPE') or 'expense').strip().lower()
        match_field = (row.get('MATCH_FIELD') or 'description').strip().lower()
        is_regex = 1 if str(row.get('IS_REGEX', '0')).strip() in ('1', 'true', 'True') else 0
        priority_raw = (row.get('PRIORITY') or '0').strip()

        if not pattern or not category_name:
            skipped += 1
            continue

        try:
            priority = int(priority_raw)
        except ValueError:
            priority = 0

        category_id = category_cache.get(normalized_key(category_name, category_type))
        if not category_id:
            skipped += 1
            errors.append(f"Rule row {idx}: category '{category_name}' ({category_type}) not found")
            continue

        existing = db.execute(
            '''SELECT id FROM category_rules
               WHERE user_id = ? AND category_id = ? AND pattern = ? AND match_field = ? AND is_regex = ?''',
            (user_id, category_id, pattern, match_field, is_regex),
        ).fetchone()

        if existing:
            db.execute(
                '''UPDATE category_rules SET priority = ? WHERE id = ? AND user_id = ?''',
                (priority, existing['id'], user_id),
            )
            updated += 1
        else:
            db.execute(
                '''INSERT INTO category_rules (user_id, category_id, pattern, match_field, is_regex, priority)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (user_id, category_id, pattern, match_field, is_regex, priority),
            )
            inserted += 1

    return inserted, updated, skipped, errors


def resolve_or_create_account_id(db, user_id, account_cache, account_name, account_currency, account_type=None):
    normalized_name = (account_name or '').strip()
    if not normalized_name:
        return None

    normalized_currency = normalize_currency(account_currency)
    if not (account_type or '').strip():
        # Older backups carry no ACCOUNT_TYPE per transaction: reuse the account
        # with this name and currency when that is unambiguous.
        prefix = normalized_key(normalized_name, normalized_currency) + '||'
        matches = {cid for key, cid in account_cache.items() if key.startswith(prefix)}
        if len(matches) == 1:
            return matches.pop()

    normalized_type = (account_type or 'checking').strip().lower()
    key = normalized_key(normalized_name, normalized_currency, normalized_type)
    existing_id = account_cache.get(key)
    if existing_id:
        return existing_id

    db.execute(
        '''INSERT INTO accounts (user_id, name, account_type, currency, balance)
           VALUES (?, ?, ?, ?, 0)''',
        (user_id, normalized_name, normalized_type, normalized_currency),
    )
    new_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    account_cache[key] = new_id
    return new_id


def resolve_category_id(category_cache, category_name, category_type):
    if not category_name:
        return None

    normalized_name = category_name.strip()
    if not normalized_name:
        return None

    normalized_type = (category_type or '').strip().lower()
    if normalized_type:
        direct = category_cache.get(normalized_key(normalized_name, normalized_type))
        if direct:
            return direct

    for key, cat_id in category_cache.items():
        name_part = key.split('||')[0]
        if name_part == normalized_name.lower():
            return cat_id
    return None


def upsert_transactions_from_rows(db, user_id, transaction_rows, account_cache, category_cache):
    imported = 0
    updated = 0
    skipped = 0
    errors = []

    for idx, row in enumerate(transaction_rows, 1):
        try:
            account_name = (row.get('ACCOUNT_NAME') or '').strip()
            account_currency = row.get('ACCOUNT_CURRENCY') or DEFAULT_BASE_CURRENCY
            account_type = row.get('ACCOUNT_TYPE') or None
            account_id = resolve_or_create_account_id(
                db, user_id, account_cache, account_name, account_currency, account_type
            )
            if not account_id:
                skipped += 1
                errors.append(f'Row {idx}: account is required')
                continue

            date_value = parse_import_date(row.get('DATE'))
            amount_value = parse_amount_string(row.get('AMOUNT'))

            transaction_type = (row.get('TRANSACTION_TYPE') or '').strip().lower()
            if transaction_type not in ('income', 'expense', 'transfer'):
                transaction_type = 'income' if amount_value >= 0 else 'expense'

            normalized_amount = abs(amount_value)
            if transaction_type == 'transfer':
                normalized_amount = abs(amount_value)

            description = (row.get('DESCRIPTION') or '').strip()
            payee = (row.get('PAYEE') or '').strip()
            category_name = (row.get('CATEGORY') or '').strip()
            category_type = (row.get('CATEGORY_TYPE') or '').strip().lower()
            category_id = resolve_category_id(category_cache, category_name, category_type)

            id_transaction = (row.get('ID_TRANSACTION') or '').strip()
            if not id_transaction:
                id_transaction = stable_transaction_id(
                    account_name, account_currency, date_value, normalized_amount, description, payee
                )

            outcome = upsert_transaction_by_external_id(
                db, user_id,
                {
                    'account_id': account_id,
                    'category_id': category_id,
                    'transaction_type': transaction_type,
                    'amount': normalized_amount,
                    'description': description,
                    'payee': payee,
                    'date': date_value,
                    'id_transaction': id_transaction,
                },
                [id_transaction],
                account_scope=account_id,
            )
            if outcome == 'inserted':
                imported += 1
            else:
                updated += 1
        except Exception as exc:
            skipped += 1
            errors.append(f'Row {idx}: {exc}')

    return imported, updated, skipped, errors


def slugify_filename(value):
    clean = re.sub(r'[^a-zA-Z0-9]+', '_', str(value or '').strip()).strip('_').lower()
    return clean or 'account'


def read_xlsx_sheet_rows(file_storage, sheet_name):
    try:
        openpyxl = importlib.import_module('openpyxl')
    except ImportError as exc:
        raise RuntimeError('openpyxl is required for XLSX import/export') from exc

    workbook = openpyxl.load_workbook(filename=io.BytesIO(file_storage.read()), data_only=True)
    if sheet_name not in workbook.sheetnames:
        return []

    sheet = workbook[sheet_name]
    values = list(sheet.values)
    if not values:
        return []

    headers = [str(col or '').strip().upper() for col in values[0]]
    rows = []
    for raw_row in values[1:]:
        row = {}
        has_content = False
        for idx, header in enumerate(headers):
            if not header:
                continue
            cell = raw_row[idx] if idx < len(raw_row) else None
            text_value = '' if cell is None else str(cell).strip()
            if text_value:
                has_content = True
            row[header] = text_value
        if has_content:
            rows.append(row)
    return rows


@app.route('/options')
@login_required
def options():
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)
    ensure_daily_rates(db, base_currency)

    rate_rows = db.execute(
        '''SELECT from_currency, to_currency, rate, provider, updated_at
           FROM exchange_rates
           WHERE to_currency = ?
           ORDER BY CASE WHEN from_currency = ? THEN 0 ELSE 1 END, from_currency ASC''',
        (base_currency, base_currency),
    ).fetchall()

    if not any(row['from_currency'] == base_currency for row in rate_rows):
        synthetic_row = {
            'from_currency': base_currency,
            'to_currency': base_currency,
            'rate': 1.0,
            'provider': 'system',
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        rate_rows = [synthetic_row] + list(rate_rows)

    return render_template(
        'options.html',
        base_currency=base_currency,
        rates=rate_rows,
        currencies=sorted(SUPPORTED_CURRENCIES),
    )


@app.route('/options/base-currency', methods=['POST'])
@login_required
def set_base_currency():
    db = get_db()
    next_currency = normalize_currency(request.form.get('base_currency', DEFAULT_BASE_CURRENCY))

    if next_currency not in SUPPORTED_CURRENCIES:
        flash('Unsupported base currency.', 'danger')
        return redirect(url_for('options'))

    db.execute(
        'UPDATE users SET base_currency = ? WHERE id = ?',
        (next_currency, current_user.id),
    )
    db.commit()
    flash(f'Base currency updated to {next_currency}.', 'success')
    return redirect(url_for('options'))


@app.route('/options/sync-rates', methods=['POST'])
@login_required
def sync_exchange_rates():
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)

    try:
        upserted = sync_rates_for_base(db, base_currency)
        db.commit()
        flash(f'Synced {upserted} ECB exchange rates for {base_currency}.', 'success')
    except Exception as exc:
        flash(f'Failed to sync ECB rates: {exc}', 'danger')

    return redirect(url_for('options'))


@app.route('/options/rates/update', methods=['POST'])
@login_required
def update_exchange_rate():
    db = get_db()
    from_currency = normalize_currency(request.form.get('from_currency', ''))
    to_currency = normalize_currency(request.form.get('to_currency', ''))
    rate_raw = request.form.get('rate', '').strip()

    if from_currency not in SUPPORTED_CURRENCIES or to_currency not in SUPPORTED_CURRENCIES:
        flash('Unsupported currency code.', 'danger')
        return redirect(url_for('options'))

    try:
        rate = float(rate_raw)
        if rate <= 0:
            raise ValueError
    except ValueError:
        flash('Rate must be a valid positive number.', 'danger')
        return redirect(url_for('options'))

    db.execute(
        '''INSERT INTO exchange_rates (from_currency, to_currency, rate, provider, updated_at)
           VALUES (?, ?, ?, 'manual', CURRENT_TIMESTAMP)
           ON CONFLICT(from_currency, to_currency)
           DO UPDATE SET rate = excluded.rate, provider = excluded.provider, updated_at = CURRENT_TIMESTAMP''',
        (from_currency, to_currency, rate),
    )
    db.commit()
    flash(f'Updated rate {from_currency} -> {to_currency}.', 'success')
    return redirect(url_for('options'))


def create_default_categories(user_id):
    db = get_db()
    default_categories = [
        ('Income', 'income', '💰', '#28a745'),
        ('Salary', 'income', '💵', '#28a745'),
        ('Groceries', 'expense', '🛒', '#dc3545'),
        ('Utilities', 'expense', '💡', '#fd7e14'),
        ('Transportation', 'expense', '🚗', '#17a2b8'),
        ('Entertainment', 'expense', '🎬', '#6f42c1'),
        ('Dining Out', 'expense', '🍽️', '#e83e8c'),
        ('Shopping', 'expense', '🛍️', '#20c997'),
        ('Healthcare', 'expense', '🏥', '#007bff'),
        ('Housing', 'expense', '🏠', '#6c757d'),
        ('Transfer', 'transfer', '↔️', '#adb5bd'),
    ]
    for name, cat_type, icon, color in default_categories:
        db.execute(
            'INSERT INTO categories (user_id, name, category_type, icon, color) VALUES (?, ?, ?, ?, ?)',
            (user_id, name, cat_type, icon, color)
        )
    db.commit()


def load_category_rules(db, user_id):
    return db.execute('''
        SELECT r.*, c.name as category_name
        FROM category_rules r
        JOIN categories c ON r.category_id = c.id
        WHERE r.user_id = ?
        ORDER BY r.priority DESC, r.id ASC
    ''', (user_id,)).fetchall()


def auto_categorize_transaction(user_id, description, payee, rules=None):
    """Try to match a transaction to a category based on rules. Pass `rules`
    (from load_category_rules) when categorizing many rows at once."""
    if rules is None:
        rules = load_category_rules(get_db(), user_id)

    for rule in rules:
        # match_field 'payee' = payee only; anything else = description and payee.
        if rule['match_field'] == 'payee':
            text_to_match = payee or ''
        else:
            text_to_match = f"{description or ''} {payee or ''}"
        if rule['is_regex']:
            try:
                # Never lowercase a regex: that turns \D \S \W into \d \s \w.
                if re.search(rule['pattern'], text_to_match, re.IGNORECASE):
                    return rule['category_id']
            except re.error:
                continue
        elif rule['pattern'].lower() in text_to_match.lower():
            return rule['category_id']

    return None


def match_import_category(user_id, rules, categories, description, payee, category_text):
    """Category for an imported row: a user rule first, else a loose name match
    against the category text the bank export provides."""
    category_id = auto_categorize_transaction(user_id, description, payee or category_text, rules=rules)
    if not category_id and category_text:
        text = category_text.lower()
        for cat in categories:
            if cat['name'].lower() in text or text in cat['name'].lower():
                return cat['id']
    return category_id


def upsert_transaction_by_external_id(db, user_id, values, match_ids, account_scope=None):
    """Insert a transaction, or refresh the existing one carrying the same external id.

    `values` holds account_id, category_id, transaction_type, amount, description,
    payee, date and id_transaction. `match_ids` are tried in order (e.g. the id and a
    legacy id); `account_scope` limits the lookup to one account (None = user-wide).
    On update the bank data (account, amount, date, text) is refreshed, but a
    category or transaction type already on the row is kept, so re-importing never
    undoes manual categorization or a row you marked as a transfer.
    Returns 'inserted', 'updated' or 'migrated' (matched through a fallback id)."""
    existing = []
    matched_index = 0
    for matched_index, external_id in enumerate(i for i in match_ids if i):
        params = [user_id, external_id]
        scope = ''
        if account_scope is not None:
            scope = ' AND account_id = ?'
            params.append(account_scope)
        existing = db.execute(
            f'SELECT id FROM transactions WHERE user_id = ? AND id_transaction = ?{scope} ORDER BY id DESC',
            params,
        ).fetchall()
        if existing:
            break

    if not existing:
        db.execute(
            '''INSERT INTO transactions
               (user_id, account_id, category_id, transaction_type, amount, description, payee, date, id_transaction)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (user_id, values['account_id'], values['category_id'], values['transaction_type'], values['amount'],
             values['description'], values['payee'], values['date'], values['id_transaction']),
        )
        return 'inserted'

    keep_id = existing[0]['id']
    db.execute(
        '''UPDATE transactions
           SET account_id = ?, category_id = COALESCE(category_id, ?), amount = ?,
               description = ?, payee = ?, date = ?, id_transaction = ?
           WHERE id = ? AND user_id = ?''',
        (values['account_id'], values['category_id'], values['amount'],
         values['description'], values['payee'], values['date'], values['id_transaction'], keep_id, user_id),
    )
    # Clean up any older duplicates for the same id.
    for duplicate_row in existing[1:]:
        db.execute('DELETE FROM transactions WHERE id = ? AND user_id = ?', (duplicate_row['id'], user_id))
    return 'migrated' if matched_index > 0 else 'updated'


def is_transfer_like_category(category_text):
    """Return True when CSV category text clearly indicates a transfer."""
    if not category_text:
        return False

    normalized = category_text.strip().lower()
    transfer_tokens = [
        'transfer',
        'transferencia',
        'transferencia interna',
        'traspaso',
        'ahorros',
        'savings',
        'internal transfer',
    ]
    return any(token in normalized for token in transfer_tokens)


def get_accounts_with_balances(db, user_id):
    """Accounts with calculated_balance = income - expense of their transactions."""
    return db.execute('''
        SELECT a.*,
            COALESCE(SUM(CASE WHEN t.transaction_type = 'income' THEN t.amount
                              WHEN t.transaction_type = 'expense' THEN -t.amount
                              ELSE 0 END), 0) as calculated_balance
        FROM accounts a
        LEFT JOIN transactions t ON t.account_id = a.id AND t.user_id = a.user_id
        WHERE a.user_id = ?
        GROUP BY a.id
        ORDER BY a.name
    ''', (user_id,)).fetchall()


def monthly_income_expense(db, user_id, base_currency, start_date=None, month=None):
    """[{month, income, expenses}] in base currency, oldest first: every month from
    start_date on, or just `month` (YYYY-MM)."""
    if month:
        condition, param = "strftime('%Y-%m', t.date) = ?", month
    else:
        condition, param = 't.date >= ?', start_date
    rows = db.execute(f'''
        SELECT strftime('%Y-%m', t.date) AS month, t.transaction_type, t.amount, a.currency AS account_currency
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ? AND t.transaction_type IN ('income', 'expense') AND {condition}
    ''', (user_id, param)).fetchall()

    totals = {}
    for row in rows:
        entry = totals.setdefault(row['month'], {'month': row['month'], 'income': 0.0, 'expenses': 0.0})
        converted = convert_amount(db, row['amount'], row['account_currency'], base_currency)
        entry['income' if row['transaction_type'] == 'income' else 'expenses'] += converted
    return [totals[key] for key in sorted(totals)]


def expense_totals_by_category(db, user_id, base_currency, month):
    """Expense spending per category for one month (YYYY-MM), largest first."""
    rows = db.execute('''
        SELECT c.id, c.name, c.icon, c.color, t.amount, a.currency AS account_currency
        FROM transactions t
        JOIN categories c ON t.category_id = c.id AND c.user_id = t.user_id
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ? AND t.transaction_type = 'expense' AND strftime('%Y-%m', t.date) = ?
    ''', (user_id, month)).fetchall()

    totals = {}
    for row in rows:
        entry = totals.setdefault(row['id'], {'name': row['name'], 'icon': row['icon'], 'color': row['color'], 'total': 0.0})
        entry['total'] += convert_amount(db, row['amount'], row['account_currency'], base_currency)
    return sorted(totals.values(), key=lambda item: item['total'], reverse=True)


def get_account_current_balance(db, user_id, account_id):
    """Compute current balance from account transactions."""
    row = db.execute('''
        SELECT COALESCE(SUM(CASE WHEN transaction_type = 'income' THEN amount
                                 WHEN transaction_type = 'expense' THEN -amount
                                 ELSE 0 END), 0) AS current_balance
        FROM transactions
        WHERE user_id = ? AND account_id = ?
    ''', (user_id, account_id)).fetchone()
    return float(row['current_balance']) if row else 0.0


class FormInputError(ValueError):
    """Invalid user input in a posted form; shown as a flash message."""


def form_amount(field, label, default=None):
    """Parse a numeric form field, rejecting text, NaN and infinity."""
    raw = (request.form.get(field) or '').strip()
    if not raw:
        if default is not None:
            return default
        raise FormInputError(f'{label} is required.')
    try:
        value = parse_amount_string(raw)
    except ValueError:
        raise FormInputError(f'{label} must be a number.')
    if value != value or value in (float('inf'), float('-inf')):
        raise FormInputError(f'{label} must be a finite number.')
    return value


@app.errorhandler(FormInputError)
def handle_form_input_error(error):
    flash(str(error), 'danger')
    return redirect(request.url)


def parse_decimal_field(raw_value, field_name, quantize_to=None):
    try:
        value = Decimal(str(raw_value if raw_value is not None else '0').strip() or '0')
    except (InvalidOperation, ValueError):
        raise ValueError(f'{field_name} must be numeric.')
    if not value.is_finite():
        raise ValueError(f'{field_name} must be a finite number.')

    if quantize_to is not None:
        value = value.quantize(quantize_to)
    return value


def get_holding_value_components(holding):
    if holding['asset_class'] == 'Mintos Lending':
        current_value = Decimal(str(holding['current_price'] or 0)).quantize(DECIMAL_MONEY_PLACES)
        invested_value = Decimal(str(holding['avg_cost'] or 0)).quantize(DECIMAL_MONEY_PLACES)
        return current_value, invested_value

    quantity = Decimal(str(holding['quantity'] or 0)).quantize(DECIMAL_QUANTITY_PLACES)
    current_price = Decimal(str(holding['current_price'] or 0)).quantize(DECIMAL_MONEY_PLACES)
    avg_cost = Decimal(str(holding['avg_cost'] or 0)).quantize(DECIMAL_MONEY_PLACES)
    current_value = (quantity * current_price).quantize(DECIMAL_MONEY_PLACES)
    invested_value = (quantity * avg_cost).quantize(DECIMAL_MONEY_PLACES)
    return current_value, invested_value


def trade_price_or_current(db, holding, trade_date, trade_price):
    """Price a holding should carry after a trade: the trade price only when the
    holding has no price yet or this is its newest trade. Back-dated or imported
    historical trades must not overwrite a newer market/evolution price."""
    current = Decimal(str(holding['current_price'] or 0)).quantize(DECIMAL_MONEY_PLACES)
    if current <= 0:
        return trade_price
    latest_trade = db.execute(
        '''SELECT MAX(operation_date) FROM investment_operations
           WHERE holding_id = ? AND operation_type IN ('buy', 'sell')''',
        (holding['id'],),
    ).fetchone()[0]
    if latest_trade is None or (trade_date or '') >= latest_trade:
        return trade_price
    return current


def get_net_contributions(db, user_id, holding_id, after_date, until_date=None):
    """Money put into a holding (deposits + buys - sell proceeds) dated after
    after_date (exclusive) and up to until_date (inclusive, open-ended if None)."""
    params = [user_id, holding_id, after_date or '']
    until_filter = ''
    if until_date:
        until_filter = 'AND operation_date <= ?'
        params.append(until_date)
    row = db.execute(
        f'''SELECT COALESCE(SUM(CASE WHEN operation_type IN ('deposit', 'buy') THEN amount
                                     WHEN operation_type = 'sell' THEN -amount
                                     ELSE 0 END), 0) AS total
            FROM investment_operations
            WHERE user_id = ? AND holding_id = ? AND operation_date > ? {until_filter}''',
        tuple(params),
    ).fetchone()
    return Decimal(str(row['total'] or 0)).quantize(DECIMAL_MONEY_PLACES)


def get_latest_valuation(db, user_id, holding_id, on_or_before=None, before=None):
    params = [user_id, holding_id]
    date_filter = ''
    if on_or_before:
        date_filter = 'AND valuation_date <= ?'
        params.append(on_or_before)
    elif before:
        date_filter = 'AND valuation_date < ?'
        params.append(before)
    return db.execute(
        f'''SELECT valuation_date, total_value
            FROM investment_valuations
            WHERE user_id = ? AND holding_id = ? {date_filter}
            ORDER BY valuation_date DESC, id DESC
            LIMIT 1''',
        tuple(params),
    ).fetchone()


def get_holding_base_value(db, user_id, holding, as_of_date=None):
    """Value the holding would have at as_of_date with zero return: the last
    valuation up to that date plus money contributed since. Without any valuation,
    the holding's current value."""
    latest = get_latest_valuation(db, user_id, holding['id'], on_or_before=as_of_date)
    if latest:
        start = Decimal(str(latest['total_value'] or 0)).quantize(DECIMAL_MONEY_PLACES)
        contributions = get_net_contributions(db, user_id, holding['id'], latest['valuation_date'], as_of_date)
        return (start + contributions).quantize(DECIMAL_MONEY_PLACES)

    current_value, _ = get_holding_value_components(holding)
    return current_value


def get_valuation_tracked_ytd(db, user_id, holding, current_value, year):
    """Year-to-date return of an evolution-tracked holding (Mintos, fixed income):
    current value minus (last valuation before this year + contributions since).
    None when the holding has no valuation before this year."""
    start = get_latest_valuation(db, user_id, holding['id'], before=f'{year}-01-01')
    if not start:
        return None
    start_value = Decimal(str(start['total_value'] or 0)).quantize(DECIMAL_MONEY_PLACES)
    contributions = get_net_contributions(db, user_id, holding['id'], start['valuation_date'])
    return current_value - (start_value + contributions)


def get_investment_accounts(db, user_id):
    return db.execute(
        '''SELECT *
           FROM accounts
           WHERE user_id = ?
             AND account_type = 'investment'
             AND COALESCE(is_investment_dashboard, 0) = 1
           ORDER BY name''',
        (user_id,),
    ).fetchall()


def build_investment_dashboard_data(db, user_id, base_currency):
    holdings = db.execute(
        '''SELECT h.*, a.name AS account_name, a.income_profile AS account_income_profile, a.currency AS account_currency
           FROM investment_holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.user_id = ?
             AND a.user_id = ?
             AND a.account_type = 'investment'
             AND COALESCE(a.is_investment_dashboard, 0) = 1
           ORDER BY h.asset_name''',
        (user_id, user_id),
    ).fetchall()

    total_portfolio_value = 0.0
    invested_capital = 0.0
    allocation_map = defaultdict(float)
    normalized_holdings = []
    current_year = datetime.now().strftime('%Y')

    operation_rows = db.execute(
        '''SELECT holding_id, operation_type, amount, realized_gain, currency, operation_date
           FROM investment_operations
           WHERE user_id = ?
             AND holding_id IS NOT NULL''',
        (user_id,),
    ).fetchall()

    all_time_map = defaultdict(float)
    yearly_map = defaultdict(float)
    for op in operation_rows:
        contribution = 0.0
        if op['operation_type'] == 'dividend':
            contribution = convert_amount(db, op['amount'] or 0, op['currency'], base_currency)
        elif op['operation_type'] == 'sell':
            contribution = convert_amount(db, op['realized_gain'] or 0, op['currency'], base_currency)

        if contribution == 0:
            continue

        holding_id = op['holding_id']
        all_time_map[holding_id] += contribution
        if (op['operation_date'] or '')[:4] == current_year:
            yearly_map[holding_id] += contribution

    for row in holdings:
        current_value_dec, cost_basis_dec = get_holding_value_components(row)
        current_value = float(current_value_dec)
        cost_basis = float(cost_basis_dec)
        converted_current = convert_amount(db, current_value, row['currency'], base_currency)
        converted_cost = convert_amount(db, cost_basis, row['currency'], base_currency)

        total_portfolio_value += converted_current
        invested_capital += converted_cost
        allocation_map[row['asset_class']] += converted_current

        item = dict(row)
        item['current_value'] = round(current_value, 2)
        item['cost_basis'] = round(cost_basis, 2)
        item['unrealized_gain'] = round(current_value - cost_basis, 2)
        item['invested_value_base'] = round(converted_cost, 2)
        item['current_value_base'] = round(converted_current, 2)
        item['unrealized_gain_base'] = round(converted_current - converted_cost, 2)

        # Evolution returns are already inside current value (valuations move the
        # price), so only cash dividends and realized sells are added on top.
        total_return_abs_base = item['unrealized_gain_base'] + float(all_time_map[row['id']])
        item['total_return_abs_base'] = round(total_return_abs_base, 2)
        item['total_return_pct'] = round(total_return_abs_base / converted_cost * 100, 6) if converted_cost > 0 else None

        created_year = (row['created_at'] or '')[:4]
        if row['asset_class'] in VALUATION_TRACKED_CLASSES:
            ytd_native = get_valuation_tracked_ytd(db, user_id, row, current_value_dec, current_year)
            if ytd_native is None:
                # No valuation before this year: the whole history is this year's.
                ytd_return_base = total_return_abs_base
            else:
                ytd_return_base = convert_amount(db, ytd_native, row['currency'], base_currency) + float(yearly_map[row['id']])
        else:
            ytd_return_base = float(yearly_map[row['id']])
            # If a holding was created this year and has no explicit yearly return rows,
            # use current total return as the YTD proxy.
            if abs(ytd_return_base) < 1e-12 and created_year == current_year:
                ytd_return_base = total_return_abs_base
        item['ytd_return_base'] = round(ytd_return_base, 2)
        item['ytd_return_pct'] = round(ytd_return_base / converted_cost * 100, 6) if converted_cost > 0 else None
        normalized_holdings.append(item)

    unrealized_gain = total_portfolio_value - invested_capital

    realized_gain = sum(
        convert_amount(db, row['realized_gain'] or 0, row['currency'], base_currency)
        for row in db.execute(
            '''SELECT realized_gain, currency
               FROM investment_operations
               WHERE user_id = ? AND operation_type = 'sell' ''',
            (user_id,),
        ).fetchall()
    )

    current_month = datetime.now().strftime('%Y-%m')
    current_year = datetime.now().strftime('%Y')
    dividend_rows = db.execute(
        '''SELECT amount, currency, operation_date
           FROM investment_operations
           WHERE user_id = ? AND operation_type = 'dividend' ''',
        (user_id,),
    ).fetchall()

    dividend_mtd = 0.0
    dividend_ytd = 0.0
    for row in dividend_rows:
        converted = convert_amount(db, row['amount'], row['currency'], base_currency)
        row_month = (row['operation_date'] or '')[:7]
        row_year = (row['operation_date'] or '')[:4]
        if row_month == current_month:
            dividend_mtd += converted
        if row_year == current_year:
            dividend_ytd += converted

    allocation_data = sorted(
        (
            {
                'asset_class': key,
                'total': value,
            }
            for key, value in allocation_map.items()
            if value > 0
        ),
        key=lambda item: item['total'],
        reverse=True,
    )

    snapshot_rows = db.execute(
        '''SELECT snapshot_date, total_value, currency
           FROM investment_snapshots
           WHERE user_id = ?
           ORDER BY snapshot_date ASC''',
        (user_id,),
    ).fetchall()

    performance_points = []
    for row in snapshot_rows:
        performance_points.append(
            {
                'date': row['snapshot_date'],
                'value': convert_amount(db, row['total_value'], row['currency'], base_currency),
            }
        )

    recent_operations = db.execute(
        '''SELECT o.*, h.asset_name, a.name AS account_name
           FROM investment_operations o
           LEFT JOIN investment_holdings h ON h.id = o.holding_id
           JOIN accounts a ON a.id = o.account_id
           WHERE o.user_id = ?
           ORDER BY o.operation_date DESC, o.id DESC
           LIMIT 15''',
        (user_id,),
    ).fetchall()

    return {
        'holdings': normalized_holdings,
        'allocation_data': allocation_data,
        'performance_points': performance_points,
        'recent_operations': recent_operations,
        'total_portfolio_value': total_portfolio_value,
        'invested_capital': invested_capital,
        'unrealized_gain': unrealized_gain,
        'realized_gain': realized_gain,
        'dividend_mtd': dividend_mtd,
        'dividend_ytd': dividend_ytd,
    }


def get_account_opening_date(account):
    """Use account creation date for balance adjustment entries."""
    created_at = (account['created_at'] or '').strip()
    if not created_at:
        return datetime.now().date().isoformat()

    # SQLite timestamps are usually YYYY-MM-DD HH:MM:SS.
    date_part = created_at.split(' ')[0]
    try:
        return datetime.strptime(date_part, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return datetime.now().date().isoformat()


def apply_target_balance_adjustment(db, user_id, account, target_balance, current_balance):
    """Insert one synthetic transaction to bridge current and target balance."""
    rounded_target = round(float(target_balance), 2)
    rounded_current = round(float(current_balance), 2)
    delta = round(rounded_target - rounded_current, 2)

    if delta == 0:
        return 0.0

    transaction_type = 'income' if delta > 0 else 'expense'
    db.execute(
        '''INSERT INTO transactions (user_id, account_id, transaction_type, amount, description, payee, date)
           VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (
            user_id,
            account['id'],
            transaction_type,
            abs(delta),
            'Balance Adjustment',
            'System Adjustment',
            get_account_opening_date(account),
        )
    )
    return delta


# User class for Flask-Login
class User(UserMixin):
    def __init__(self, id, username, email):
        self.id = id
        self.username = username
        self.email = email


@login_manager.user_loader
def load_user(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    if user:
        return User(user['id'], user['username'], user['email'])
    return None


# Authentication routes
def registration_open():
    if app.config['REGISTRATION_ENABLED']:
        return True
    return get_db().execute('SELECT 1 FROM users LIMIT 1').fetchone() is None


def safe_next_url(target):
    """Only allow same-site relative redirects after login."""
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


app.jinja_env.globals['registration_open'] = registration_open


@app.route('/register', methods=['GET', 'POST'])
def register():
    if not registration_open():
        flash('Registration is currently disabled.', 'warning')
        return redirect(url_for('login'))
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']
        confirm_password = request.form['confirm_password']

        db = get_db()
        error = None

        if not username:
            error = 'Username is required.'
        elif not email:
            error = 'Email is required.'
        elif not password:
            error = 'Password is required.'
        elif password != confirm_password:
            error = 'Passwords do not match.'
        elif len(password) < 6:
            error = 'Password must be at least 6 characters.'
        elif db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone():
            error = 'Username already exists.'
        elif db.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone():
            error = 'Email already registered.'

        if error is None:
            password_hash = hash_password(password)
            cursor = db.execute(
                'INSERT INTO users (username, email, password_hash) VALUES (?, ?, ?)',
                (username, email, password_hash)
            )
            db.commit()
            user_id = cursor.lastrowid
            create_default_categories(user_id)
            flash('Registration successful! Please log in.', 'success')
            return redirect(url_for('login'))

        flash(error, 'danger')

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

        if user and check_password_hash(user['password_hash'], password):
            user_obj = User(user['id'], user['username'], user['email'])
            login_user(user_obj)
            next_page = safe_next_url(request.args.get('next'))
            return redirect(next_page or url_for('dashboard'))

        flash('Invalid username or password.', 'danger')

    return render_template('login.html')


@app.route('/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# Main routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/health')
def health_check():
    """Health check endpoint for Docker/load balancer."""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})


@app.route('/dashboard')
@login_required
def dashboard():
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)
    ensure_daily_rates(db, base_currency)

    accounts = get_accounts_with_balances(db, current_user.id)

    # Calculate total balance in user base currency.
    total_balance = sum(
        convert_amount(db, acc['calculated_balance'], acc['currency'], base_currency)
        for acc in accounts
    )

    # Get recent transactions
    recent_transactions = db.execute('''
         SELECT t.*, a.name as account_name, a.currency as account_currency,
             c.name as category_name, c.icon as category_icon
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ?
        ORDER BY t.date DESC, t.id DESC
        LIMIT 10
    ''', (current_user.id,)).fetchall()

    current_month = datetime.now().strftime('%Y-%m')
    this_month = monthly_income_expense(db, current_user.id, base_currency, month=current_month)
    monthly_income = this_month[0]['income'] if this_month else 0.0
    monthly_expenses = this_month[0]['expenses'] if this_month else 0.0

    category_spending = [
        item for item in expense_totals_by_category(db, current_user.id, base_currency, current_month)
        if item['total'] > 0
    ][:5]

    return render_template('dashboard.html',
                         accounts=accounts,
                         total_balance=total_balance,
                         recent_transactions=recent_transactions,
                         monthly_income=monthly_income,
                         monthly_expenses=monthly_expenses,
                         category_spending=category_spending,
                         base_currency=base_currency)


# Account routes
@app.route('/accounts')
@login_required
def accounts():
    db = get_db()
    return render_template('accounts.html', accounts=get_accounts_with_balances(db, current_user.id))


@app.route('/accounts/add', methods=['GET', 'POST'])
@login_required
def add_account():
    if request.method == 'POST':
        name = request.form['name'].strip()
        account_type = request.form['account_type']
        initial_balance = form_amount('initial_balance', 'Initial balance', default=0.0)
        currency = normalize_currency(request.form.get('currency') or get_user_base_currency(current_user.id))

        if not name:
            flash('Account name is required.', 'danger')
            return render_template('account_form.html', account=None)

        db = get_db()
        cursor = db.execute(
            'INSERT INTO accounts (user_id, name, account_type, balance, currency) VALUES (?, ?, ?, ?, ?)',
            (current_user.id, name, account_type, initial_balance, currency)
        )
        account_id = cursor.lastrowid

        # Add initial balance as a transaction if non-zero
        if initial_balance != 0:
            trans_type = 'income' if initial_balance > 0 else 'expense'
            db.execute(
                '''INSERT INTO transactions (user_id, account_id, transaction_type, amount, description, date)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (current_user.id, account_id, trans_type, abs(initial_balance), 'Opening Balance', datetime.now().date().isoformat())
            )

        db.commit()
        flash('Account created successfully!', 'success')
        return redirect(url_for('accounts'))

    return render_template('account_form.html', account=None)


@app.route('/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_account(account_id):
    db = get_db()
    account = db.execute('SELECT * FROM accounts WHERE id = ? AND user_id = ?',
                        (account_id, current_user.id)).fetchone()

    if not account:
        flash('Account not found.', 'danger')
        return redirect(url_for('accounts'))

    current_balance = get_account_current_balance(db, current_user.id, account_id)

    if request.method == 'POST':
        name = request.form['name'].strip()
        account_type = request.form['account_type']
        currency = normalize_currency(request.form.get('currency') or get_user_base_currency(current_user.id))
        change_current_balance = request.form.get('change_current_balance') == 'on'
        target_balance_raw = request.form.get('target_current_balance', '').strip()

        if not name:
            flash('Account name is required.', 'danger')
            return render_template('account_form.html', account=account, current_balance=current_balance)

        adjustment_delta = 0.0
        if change_current_balance:
            if not target_balance_raw:
                flash('Target current balance is required when balance change is enabled.', 'danger')
                return render_template('account_form.html', account=account, current_balance=current_balance)
            try:
                target_balance = round(float(target_balance_raw), 2)
            except ValueError:
                flash('Target current balance must be a valid number.', 'danger')
                return render_template('account_form.html', account=account, current_balance=current_balance)

        db.execute(
            'UPDATE accounts SET name = ?, account_type = ?, currency = ? WHERE id = ? AND user_id = ?',
            (name, account_type, currency, account_id, current_user.id)
        )

        if change_current_balance:
            adjustment_delta = apply_target_balance_adjustment(
                db,
                current_user.id,
                account,
                target_balance,
                current_balance,
            )

        db.commit()

        if change_current_balance and adjustment_delta != 0:
            flash(f'Account updated and balance adjusted by {adjustment_delta:.2f}.', 'success')
        else:
            flash('Account updated successfully!', 'success')

        return redirect(url_for('accounts'))

    return render_template('account_form.html', account=account, current_balance=current_balance)


def delete_account_with_data(db, user_id, account_id):
    """Delete an account and everything hanging off it (foreign keys are not
    enforced, so nothing else would clean these rows up)."""
    holding_ids = [row['id'] for row in db.execute(
        'SELECT id FROM investment_holdings WHERE user_id = ? AND account_id = ?', (user_id, account_id)
    )]
    for holding_id in holding_ids:
        db.execute('DELETE FROM investment_valuations WHERE user_id = ? AND holding_id = ?', (user_id, holding_id))
    db.execute('DELETE FROM investment_operations WHERE user_id = ? AND account_id = ?', (user_id, account_id))
    db.execute('DELETE FROM investment_holdings WHERE user_id = ? AND account_id = ?', (user_id, account_id))
    db.execute('DELETE FROM transactions WHERE user_id = ? AND account_id = ?', (user_id, account_id))
    db.execute('DELETE FROM accounts WHERE id = ? AND user_id = ?', (account_id, user_id))


@app.route('/accounts/<int:account_id>/delete', methods=['POST'])
@login_required
def delete_account(account_id):
    db = get_db()
    delete_account_with_data(db, current_user.id, account_id)
    db.commit()
    flash('Account deleted.', 'info')
    return redirect(url_for('accounts'))


@app.route('/investments')
@login_required
def investments():
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)
    ensure_daily_rates(db, base_currency)

    investment_accounts = get_investment_accounts(db, current_user.id)
    dashboard_data = build_investment_dashboard_data(db, current_user.id, base_currency)

    return render_template(
        'investments.html',
        investment_accounts=investment_accounts,
        base_currency=base_currency,
        asset_classes=INVESTMENT_ASSET_CLASSES,
        market_syncable_classes=INVESTMENT_MARKET_SYNCABLE_CLASSES,
        today=datetime.now().strftime('%Y-%m-%d'),
        **dashboard_data,
    )


@app.route('/investments/accounts/add', methods=['GET', 'POST'])
@login_required
def add_investment_account():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        currency = normalize_currency(request.form.get('currency', 'EUR'))
        income_profile = (request.form.get('income_profile', '') or '').strip().lower()

        if not name:
            flash('Investment account name is required.', 'danger')
            return render_template('investment_account_form.html', account=None, profiles=INVESTMENT_INCOME_PROFILES)
        if currency not in SUPPORTED_CURRENCIES:
            flash('Unsupported currency.', 'danger')
            return render_template('investment_account_form.html', account=None, profiles=INVESTMENT_INCOME_PROFILES)
        if income_profile not in INVESTMENT_INCOME_PROFILES:
            flash('Please select fixed or variable income profile.', 'danger')
            return render_template('investment_account_form.html', account=None, profiles=INVESTMENT_INCOME_PROFILES)

        db = get_db()
        db.execute(
            '''INSERT INTO accounts (user_id, name, account_type, balance, currency, income_profile, is_investment_dashboard)
               VALUES (?, ?, 'investment', 0, ?, ?, 1)''',
            (current_user.id, name, currency, income_profile),
        )
        db.commit()
        flash('Investment account created successfully.', 'success')
        return redirect(url_for('investments'))

    return render_template('investment_account_form.html', account=None, profiles=INVESTMENT_INCOME_PROFILES)


@app.route('/investments/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_investment_account(account_id):
    db = get_db()
    account = db.execute(
        '''SELECT *
           FROM accounts
           WHERE id = ?
             AND user_id = ?
             AND account_type = 'investment'
             AND COALESCE(is_investment_dashboard, 0) = 1''',
        (account_id, current_user.id),
    ).fetchone()

    if not account:
        flash('Investment account not found.', 'danger')
        return redirect(url_for('investments'))

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        currency = normalize_currency(request.form.get('currency', 'EUR'))
        income_profile = (request.form.get('income_profile', '') or '').strip().lower()

        if not name:
            flash('Investment account name is required.', 'danger')
            return render_template('investment_account_form.html', account=account, profiles=INVESTMENT_INCOME_PROFILES)
        if currency not in SUPPORTED_CURRENCIES:
            flash('Unsupported currency.', 'danger')
            return render_template('investment_account_form.html', account=account, profiles=INVESTMENT_INCOME_PROFILES)
        if income_profile not in INVESTMENT_INCOME_PROFILES:
            flash('Please select fixed or variable income profile.', 'danger')
            return render_template('investment_account_form.html', account=account, profiles=INVESTMENT_INCOME_PROFILES)

        db.execute(
            '''UPDATE accounts
               SET name = ?, currency = ?, income_profile = ?
               WHERE id = ? AND user_id = ?''',
            (name, currency, income_profile, account_id, current_user.id),
        )
        db.commit()
        flash('Investment account updated successfully.', 'success')
        return redirect(url_for('investments'))

    return render_template('investment_account_form.html', account=account, profiles=INVESTMENT_INCOME_PROFILES)


@app.route('/investments/accounts/<int:account_id>/delete', methods=['POST'])
@login_required
def delete_investment_account(account_id):
    db = get_db()
    account = db.execute(
        '''SELECT id
           FROM accounts
           WHERE id = ?
             AND user_id = ?
             AND account_type = 'investment'
             AND COALESCE(is_investment_dashboard, 0) = 1''',
        (account_id, current_user.id),
    ).fetchone()

    if not account:
        flash('Investment account not found.', 'danger')
        return redirect(url_for('investments'))

    linked_holding_count = db.execute(
        'SELECT COUNT(*) AS c FROM investment_holdings WHERE user_id = ? AND account_id = ?',
        (current_user.id, account_id),
    ).fetchone()['c']
    if linked_holding_count:
        flash('Delete holdings in this account first.', 'warning')
        return redirect(url_for('investments'))

    delete_account_with_data(db, current_user.id, account_id)
    db.commit()
    flash('Investment account deleted.', 'info')
    return redirect(url_for('investments'))


@app.route('/investments/holdings/add', methods=['GET', 'POST'])
@login_required
def add_investment_holding():
    db = get_db()
    investment_accounts = get_investment_accounts(db, current_user.id)

    if not investment_accounts:
        flash('Create an investment account first.', 'warning')
        return redirect(url_for('add_investment_account'))

    if request.method == 'POST':
        account_id = request.form.get('account_id', type=int)
        asset_name = request.form.get('asset_name', '').strip()
        ticker = request.form.get('ticker', '').strip().upper()
        isin = normalize_isin(request.form.get('isin', ''))
        asset_class = request.form.get('asset_class', '').strip()
        currency = normalize_currency(request.form.get('currency', 'EUR'))

        try:
            quantity = parse_decimal_field(request.form.get('quantity', 0), 'Quantity', DECIMAL_QUANTITY_PLACES)
            avg_cost = parse_decimal_field(request.form.get('avg_cost', 0), 'Average cost', DECIMAL_MONEY_PLACES)
            current_price = parse_decimal_field(request.form.get('current_price', 0), 'Current price', DECIMAL_MONEY_PLACES)
        except ValueError:
            flash('Quantity and prices must be numeric.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=None,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )

        valid_account = any(acc['id'] == account_id for acc in investment_accounts)
        if not valid_account:
            flash('Invalid investment account selected.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=None,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if not asset_name:
            flash('Asset name is required.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=None,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if asset_class not in INVESTMENT_ASSET_CLASSES:
            flash('Invalid asset class selected.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=None,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if asset_class == 'IF' and not isin:
            flash('IF holdings require an ISIN.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=None,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if asset_class == 'Mintos Lending':
            quantity = Decimal('1').quantize(DECIMAL_QUANTITY_PLACES)

        if quantity < 0 or avg_cost < 0 or current_price < 0:
            flash('Quantity and prices cannot be negative.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=None,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )

        db.execute(
            '''INSERT INTO investment_holdings
               (user_id, account_id, asset_name, ticker, isin, asset_class, currency, quantity, avg_cost, current_price, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)''',
            (current_user.id, account_id, asset_name, ticker, isin, asset_class, currency, float(quantity), float(avg_cost), float(current_price)),
        )
        db.commit()
        flash('Holding added successfully.', 'success')
        return redirect(url_for('investments'))

    return render_template(
        'investment_holding_form.html',
        holding=None,
        investment_accounts=investment_accounts,
        asset_classes=INVESTMENT_ASSET_CLASSES,
    )


@app.route('/investments/holdings/<int:holding_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_investment_holding(holding_id):
    db = get_db()
    holding = db.execute(
        '''SELECT h.*
           FROM investment_holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.id = ?
             AND h.user_id = ?
             AND a.user_id = ?
             AND a.account_type = 'investment'
             AND COALESCE(a.is_investment_dashboard, 0) = 1''',
        (holding_id, current_user.id, current_user.id),
    ).fetchone()

    if not holding:
        flash('Holding not found.', 'danger')
        return redirect(url_for('investments'))

    investment_accounts = get_investment_accounts(db, current_user.id)

    if request.method == 'POST':
        account_id = request.form.get('account_id', type=int)
        asset_name = request.form.get('asset_name', '').strip()
        ticker = request.form.get('ticker', '').strip().upper()
        isin = normalize_isin(request.form.get('isin', ''))
        asset_class = request.form.get('asset_class', '').strip()
        currency = normalize_currency(request.form.get('currency', 'EUR'))

        try:
            quantity = parse_decimal_field(request.form.get('quantity', 0), 'Quantity', DECIMAL_QUANTITY_PLACES)
            avg_cost = parse_decimal_field(request.form.get('avg_cost', 0), 'Average cost', DECIMAL_MONEY_PLACES)
            current_price = parse_decimal_field(request.form.get('current_price', 0), 'Current price', DECIMAL_MONEY_PLACES)
        except ValueError:
            flash('Quantity and prices must be numeric.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=holding,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )

        valid_account = any(acc['id'] == account_id for acc in investment_accounts)
        if not valid_account:
            flash('Invalid investment account selected.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=holding,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if not asset_name:
            flash('Asset name is required.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=holding,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if asset_class not in INVESTMENT_ASSET_CLASSES:
            flash('Invalid asset class selected.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=holding,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if asset_class == 'IF' and not isin:
            flash('IF holdings require an ISIN.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=holding,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )
        if asset_class == 'Mintos Lending':
            quantity = Decimal('1').quantize(DECIMAL_QUANTITY_PLACES)

        if quantity < 0 or avg_cost < 0 or current_price < 0:
            flash('Quantity and prices cannot be negative.', 'danger')
            return render_template(
                'investment_holding_form.html',
                holding=holding,
                investment_accounts=investment_accounts,
                asset_classes=INVESTMENT_ASSET_CLASSES,
            )

        db.execute(
            '''UPDATE investment_holdings
               SET account_id = ?,
                   asset_name = ?,
                   ticker = ?,
                   isin = ?,
                   asset_class = ?,
                   currency = ?,
                   quantity = ?,
                   avg_cost = ?,
                   current_price = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?''',
            (
                account_id,
                asset_name,
                ticker,
                isin,
                asset_class,
                currency,
                float(quantity),
                float(avg_cost),
                float(current_price),
                holding_id,
                current_user.id,
            ),
        )
        db.commit()
        flash('Holding updated successfully.', 'success')
        return redirect(url_for('investments'))

    return render_template(
        'investment_holding_form.html',
        holding=holding,
        investment_accounts=investment_accounts,
        asset_classes=INVESTMENT_ASSET_CLASSES,
    )


@app.route('/investments/holdings/<int:holding_id>/delete', methods=['POST'])
@login_required
def delete_investment_holding(holding_id):
    db = get_db()
    holding = db.execute('SELECT id FROM investment_holdings WHERE id = ? AND user_id = ?',
                         (holding_id, current_user.id)).fetchone()
    if not holding:
        flash('Holding not found.', 'danger')
        return redirect(url_for('investments'))

    db.execute('DELETE FROM investment_valuations WHERE holding_id = ? AND user_id = ?',
               (holding_id, current_user.id))
    db.execute('DELETE FROM investment_operations WHERE holding_id = ? AND user_id = ?',
               (holding_id, current_user.id))
    db.execute('DELETE FROM investment_holdings WHERE id = ? AND user_id = ?',
               (holding_id, current_user.id))
    db.commit()
    flash('Holding deleted.', 'info')
    return redirect(url_for('investments'))


@app.route('/investments/holdings/<int:holding_id>/sync-price', methods=['POST'])
@login_required
def sync_investment_holding_price(holding_id):
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)

    holding = db.execute(
        '''SELECT h.*, a.user_id AS account_user_id
           FROM investment_holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.id = ? AND h.user_id = ? AND a.user_id = ?''',
        (holding_id, current_user.id, current_user.id),
    ).fetchone()

    if not holding:
        flash('Holding not found.', 'danger')
        return redirect(url_for('investments'))

    if holding['asset_class'] not in INVESTMENT_MARKET_SYNCABLE_CLASSES:
        flash('This asset class is not eligible for market sync.', 'warning')
        return redirect(url_for('edit_investment_holding', holding_id=holding_id))

    identifiers = resolve_holding_market_identifiers(holding)
    if not identifiers:
        flash('Cannot sync price without ticker/ISIN.', 'warning')
        return redirect(url_for('edit_investment_holding', holding_id=holding_id))

    try:
        quote = None
        last_exc = None
        for identifier in identifiers:
            try:
                quote = fetch_market_price(identifier)
                break
            except Exception as exc:
                last_exc = exc
                continue

        if quote is None:
            raise last_exc or ValueError('No provider returned a market price.')

        synced_price = convert_quote_to_holding_currency(
            db,
            quote['price'],
            quote.get('currency'),
            holding['currency'],
            base_currency,
        )
        db.execute(
            '''UPDATE investment_holdings
               SET current_price = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?''',
            (float(round(synced_price, 8)), holding_id, current_user.id),
        )
        db.commit()
        flash(f'Price synced from {quote["source"]}: {holding["currency"]} {synced_price:,.4f}', 'success')
    except Exception as exc:
        flash(f'Price sync failed: {exc}', 'warning')

    return redirect(url_for('edit_investment_holding', holding_id=holding_id))


@app.route('/investments/sync-prices', methods=['POST'])
@login_required
def sync_all_investment_prices():
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)

    holdings = db.execute(
        '''SELECT h.*
           FROM investment_holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.user_id = ?
             AND a.user_id = ?
             AND a.account_type = 'investment'
             AND COALESCE(a.is_investment_dashboard, 0) = 1
           ORDER BY h.asset_name''',
        (current_user.id, current_user.id),
    ).fetchall()

    updated = 0
    skipped = []
    failed = []

    for holding in holdings:
        if holding['asset_class'] not in INVESTMENT_MARKET_SYNCABLE_CLASSES:
            skipped.append(f"{holding['asset_name']} (ineligible class)")
            continue

        identifiers = resolve_holding_market_identifiers(holding)
        if not identifiers:
            skipped.append(f"{holding['asset_name']} (missing ticker/ISIN)")
            continue

        try:
            quote = None
            last_exc = None
            for identifier in identifiers:
                try:
                    quote = fetch_market_price(identifier)
                    break
                except Exception as exc:
                    last_exc = exc
                    continue

            if quote is None:
                raise last_exc or ValueError('No provider returned a market price.')

            synced_price = convert_quote_to_holding_currency(
                db,
                quote['price'],
                quote.get('currency'),
                holding['currency'],
                base_currency,
            )
            db.execute(
                '''UPDATE investment_holdings
                   SET current_price = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND user_id = ?''',
                (float(round(synced_price, 8)), holding['id'], current_user.id),
            )
            updated += 1
        except Exception as exc:
            failed.append(f"{holding['asset_name']}: {exc}")

    db.commit()

    if updated:
        flash(f'Updated market prices for {updated} holding(s).', 'success')
    if skipped:
        flash(f'Skipped {len(skipped)} holding(s): ' + '; '.join(skipped[:5]), 'warning')
    if failed:
        flash(f'Failed {len(failed)} holding(s): ' + '; '.join(failed[:5]), 'warning')
    if not (updated or skipped or failed):
        flash('No holdings available for market sync.', 'info')

    return redirect(url_for('investments'))


@app.route('/investments/operations/add', methods=['GET', 'POST'])
@login_required
def add_investment_operation():
    db = get_db()
    investment_accounts = get_investment_accounts(db, current_user.id)
    holdings = db.execute(
        '''SELECT h.*, a.name AS account_name
           FROM investment_holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.user_id = ?
             AND a.user_id = ?
             AND a.account_type = 'investment'
             AND COALESCE(a.is_investment_dashboard, 0) = 1
           ORDER BY h.asset_name''',
        (current_user.id, current_user.id),
    ).fetchall()

    if request.method == 'POST':
        account_id = request.form.get('account_id', type=int)
        holding_id = request.form.get('holding_id', type=int)
        operation_type = (request.form.get('operation_type', '') or '').strip().lower()
        note = request.form.get('note', '').strip()
        currency = normalize_currency(request.form.get('currency', 'USD'))
        operation_date = request.form.get('operation_date')
        is_reinvested = request.form.get('is_reinvested') == 'on'

        try:
            quantity = parse_decimal_field(request.form.get('quantity', 0), 'Quantity', DECIMAL_QUANTITY_PLACES)
            price_per_unit = parse_decimal_field(request.form.get('price_per_unit', 0), 'Price per unit', DECIMAL_MONEY_PLACES)
            amount = parse_decimal_field(request.form.get('amount', 0), 'Amount', DECIMAL_MONEY_PLACES)
        except ValueError:
            flash('Quantity, price and amount must be numeric.', 'danger')
            return render_template(
                'investment_operation_form.html',
                investment_accounts=investment_accounts,
                holdings=holdings,
                today=datetime.now().strftime('%Y-%m-%d'),
            )

        valid_account = any(acc['id'] == account_id for acc in investment_accounts)
        if not valid_account:
            flash('Invalid investment account selected.', 'danger')
            return render_template(
                'investment_operation_form.html',
                investment_accounts=investment_accounts,
                holdings=holdings,
                today=datetime.now().strftime('%Y-%m-%d'),
            )

        holding = None
        if holding_id:
            holding = db.execute(
                '''SELECT * FROM investment_holdings
                   WHERE id = ? AND user_id = ? AND account_id = ?''',
                (holding_id, current_user.id, account_id),
            ).fetchone()
            if not holding:
                flash('Selected holding does not belong to this account.', 'danger')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )

        if operation_type not in ('buy', 'sell', 'dividend', 'deposit'):
            flash('Invalid operation type.', 'danger')
            return render_template(
                'investment_operation_form.html',
                investment_accounts=investment_accounts,
                holdings=holdings,
                today=datetime.now().strftime('%Y-%m-%d'),
            )

        if not operation_date:
            flash('Operation date is required.', 'danger')
            return render_template(
                'investment_operation_form.html',
                investment_accounts=investment_accounts,
                holdings=holdings,
                today=datetime.now().strftime('%Y-%m-%d'),
            )

        realized_gain = None
        if operation_type in ('buy', 'sell'):
            if not holding:
                flash('Buy and sell operations require selecting a holding.', 'danger')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )
            if holding['asset_class'] == 'Mintos Lending':
                flash('Mintos Lending is treated as account money. Use Evolution / Auto Returns instead of buy/sell.', 'warning')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )
            if quantity <= 0 or price_per_unit <= 0:
                flash('Quantity and price per unit must be positive.', 'danger')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )

            amount = (quantity * price_per_unit).quantize(DECIMAL_MONEY_PLACES)

            old_qty = Decimal(str(holding['quantity'] or 0)).quantize(DECIMAL_QUANTITY_PLACES)
            old_avg = Decimal(str(holding['avg_cost'] or 0)).quantize(DECIMAL_MONEY_PLACES)
            # avg_cost/current_price are in the holding's currency; the trade may not be.
            to_holding_fx = Decimal(str(convert_amount(db, 1, currency, holding['currency'])))
            price_in_holding_ccy = (price_per_unit * to_holding_fx).quantize(DECIMAL_MONEY_PLACES)
            new_price = trade_price_or_current(db, holding, operation_date, price_in_holding_ccy)

            if operation_type == 'buy':
                new_qty = (old_qty + quantity).quantize(DECIMAL_QUANTITY_PLACES)
                new_avg = (((old_qty * old_avg) + (quantity * price_in_holding_ccy)) / new_qty).quantize(DECIMAL_MONEY_PLACES) if new_qty > 0 else Decimal('0')
                db.execute(
                    '''UPDATE investment_holdings
                       SET quantity = ?, avg_cost = ?, current_price = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND user_id = ?''',
                    (float(new_qty), float(new_avg), float(new_price), holding['id'], current_user.id),
                )
            else:
                if quantity - old_qty > FRACTIONAL_TOLERANCE:
                    flash('Sell quantity cannot exceed current holding quantity.', 'danger')
                    return render_template(
                        'investment_operation_form.html',
                        investment_accounts=investment_accounts,
                        holdings=holdings,
                        today=datetime.now().strftime('%Y-%m-%d'),
                    )
                new_qty = old_qty - quantity
                if abs(new_qty) <= FRACTIONAL_TOLERANCE:
                    new_qty = Decimal('0')
                new_qty = new_qty.quantize(DECIMAL_QUANTITY_PLACES)
                # Stored in the operation's currency, like the operation amount.
                old_avg_in_trade_ccy = old_avg / to_holding_fx if to_holding_fx else old_avg
                realized_gain = ((price_per_unit - old_avg_in_trade_ccy) * quantity).quantize(DECIMAL_MONEY_PLACES)
                db.execute(
                    '''UPDATE investment_holdings
                       SET quantity = ?, current_price = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND user_id = ?''',
                    (float(new_qty), float(new_price), holding['id'], current_user.id),
                )

        elif operation_type == 'dividend':
            if amount <= 0:
                flash('Dividend amount must be positive.', 'danger')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )

            if is_reinvested:
                if not holding:
                    flash('Reinvested dividends require a holding.', 'danger')
                    return render_template(
                        'investment_operation_form.html',
                        investment_accounts=investment_accounts,
                        holdings=holdings,
                        today=datetime.now().strftime('%Y-%m-%d'),
                    )
                if price_per_unit <= 0:
                    flash('Reinvested dividends require a positive reinvest price.', 'danger')
                    return render_template(
                        'investment_operation_form.html',
                        investment_accounts=investment_accounts,
                        holdings=holdings,
                        today=datetime.now().strftime('%Y-%m-%d'),
                    )

                if holding['asset_class'] == 'Mintos Lending':
                    flash('Mintos Lending does not use dividend reinvest share logic. Use Deposit or Evolution instead.', 'warning')
                    return render_template(
                        'investment_operation_form.html',
                        investment_accounts=investment_accounts,
                        holdings=holdings,
                        today=datetime.now().strftime('%Y-%m-%d'),
                    )

                old_qty = Decimal(str(holding['quantity'] or 0)).quantize(DECIMAL_QUANTITY_PLACES)
                old_avg = Decimal(str(holding['avg_cost'] or 0)).quantize(DECIMAL_MONEY_PLACES)
                extra_qty = (amount / price_per_unit).quantize(DECIMAL_QUANTITY_PLACES)
                new_qty = (old_qty + extra_qty).quantize(DECIMAL_QUANTITY_PLACES)
                new_avg = (((old_qty * old_avg) + amount) / new_qty).quantize(DECIMAL_MONEY_PLACES) if new_qty > 0 else Decimal('0')
                db.execute(
                    '''UPDATE investment_holdings
                       SET quantity = ?, avg_cost = ?, current_price = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ? AND user_id = ?''',
                    (float(new_qty), float(new_avg), float(price_per_unit), holding['id'], current_user.id),
                )

        elif operation_type == 'deposit':
            if not holding:
                flash('Deposit operation requires selecting a holding.', 'danger')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )

            if holding['asset_class'] != 'Mintos Lending':
                flash('Deposit operation is currently only for Mintos Lending holdings.', 'danger')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )

            if amount <= 0:
                flash('Deposit amount must be positive.', 'danger')
                return render_template(
                    'investment_operation_form.html',
                    investment_accounts=investment_accounts,
                    holdings=holdings,
                    today=datetime.now().strftime('%Y-%m-%d'),
                )

            old_invested = Decimal(str(holding['avg_cost'] or 0)).quantize(DECIMAL_MONEY_PLACES)
            old_value = Decimal(str(holding['current_price'] or 0)).quantize(DECIMAL_MONEY_PLACES)
            new_invested = (old_invested + amount).quantize(DECIMAL_MONEY_PLACES)
            new_value = (old_value + amount).quantize(DECIMAL_MONEY_PLACES)

            db.execute(
                '''UPDATE investment_holdings
                   SET quantity = ?, avg_cost = ?, current_price = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND user_id = ?''',
                (1.0, float(new_invested), float(new_value), holding['id'], current_user.id),
            )

            quantity = Decimal('0')
            price_per_unit = Decimal('0')

        db.execute(
            '''INSERT INTO investment_operations
               (user_id, account_id, holding_id, operation_type, quantity, price_per_unit, amount, currency,
                is_reinvested, realized_gain, note, operation_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                current_user.id,
                account_id,
                holding['id'] if holding else None,
                operation_type,
                float(quantity) if quantity > 0 else None,
                float(price_per_unit) if price_per_unit > 0 else None,
                float(amount),
                currency,
                1 if is_reinvested else 0,
                float(realized_gain) if realized_gain is not None else None,
                note,
                operation_date,
            ),
        )

        db.commit()
        flash('Investment operation saved.', 'success')
        return redirect(url_for('investments'))

    return render_template(
        'investment_operation_form.html',
        investment_accounts=investment_accounts,
        holdings=holdings,
        today=datetime.now().strftime('%Y-%m-%d'),
    )


@app.route('/investments/snapshots/add', methods=['POST'])
@login_required
def add_investment_snapshot():
    db = get_db()
    snapshot_date = request.form.get('snapshot_date', '').strip()
    currency = normalize_currency(request.form.get('currency', DEFAULT_BASE_CURRENCY))

    try:
        total_value = float(request.form.get('total_value', 0) or 0)
    except ValueError:
        flash('Snapshot total value must be numeric.', 'danger')
        return redirect(url_for('investments'))

    if not snapshot_date:
        flash('Snapshot date is required.', 'danger')
        return redirect(url_for('investments'))
    if total_value <= 0:
        flash('Snapshot value must be greater than zero.', 'danger')
        return redirect(url_for('investments'))
    if currency not in SUPPORTED_CURRENCIES:
        flash('Unsupported snapshot currency.', 'danger')
        return redirect(url_for('investments'))

    db.execute(
        '''INSERT INTO investment_snapshots (user_id, snapshot_date, total_value, currency)
           VALUES (?, ?, ?, ?)''',
        (current_user.id, snapshot_date, total_value, currency),
    )
    db.commit()
    flash('Portfolio snapshot added.', 'success')
    return redirect(url_for('investments'))


def apply_mintos_evolution(db, user_id, holding, valuation_date, total_value, note='', base_value=None):
    """Set a Mintos Lending holding's current total value at a date: record a
    valuation whose return_amount is the change vs the base value (the previous
    valuation plus deposits since). Shared by the manual evolution route and the
    statement importer."""
    if base_value is None:
        base_value = get_holding_base_value(db, user_id, holding, valuation_date)
    total_value = total_value.quantize(DECIMAL_MONEY_PLACES)
    return_amount = (total_value - base_value).quantize(DECIMAL_MONEY_PLACES)
    return_pct = None
    if base_value > 0:
        return_pct = ((return_amount / base_value) * Decimal('100')).quantize(DECIMAL_MONEY_PLACES)

    db.execute(
        '''INSERT INTO investment_valuations
           (user_id, holding_id, valuation_date, total_value, currency, return_pct, return_amount, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            user_id,
            holding['id'],
            valuation_date,
            float(total_value),
            holding['currency'],
            float(return_pct) if return_pct is not None else None,
            float(return_amount),
            note,
        ),
    )
    db.execute(
        '''UPDATE investment_holdings
           SET quantity = 1, current_price = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND user_id = ?''',
        (float(total_value), holding['id'], user_id),
    )
    return {'total_value': total_value, 'return_amount': return_amount, 'return_pct': return_pct}


@app.route('/investments/evolution/add', methods=['POST'])
@login_required
def add_investment_evolution():
    db = get_db()
    holding_id = request.form.get('holding_id', type=int)
    valuation_date = (request.form.get('valuation_date') or '').strip()
    total_value_raw = request.form.get('total_value', '')
    return_pct_raw = request.form.get('return_pct', '')
    note = (request.form.get('note') or '').strip()

    holding = db.execute(
        '''SELECT h.*, a.id AS account_id
           FROM investment_holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.id = ?
             AND h.user_id = ?
             AND a.user_id = ?
             AND a.account_type = 'investment'
             AND COALESCE(a.is_investment_dashboard, 0) = 1''',
        (holding_id, current_user.id, current_user.id),
    ).fetchone()

    if not holding:
        flash('Invalid holding selected.', 'danger')
        return redirect(url_for('investments'))

    if not valuation_date:
        flash('Valuation date is required.', 'danger')
        return redirect(url_for('investments'))

    base_value = get_holding_base_value(db, current_user.id, holding, valuation_date)
    quantity = Decimal(str(holding['quantity'] or 0)).quantize(DECIMAL_QUANTITY_PLACES)

    holding_class = holding['asset_class']
    computed_total_value = None
    computed_return_amount = None
    computed_return_pct = None

    if holding_class == 'Mintos Lending':
        try:
            computed_total_value = parse_decimal_field(total_value_raw, 'Total value', DECIMAL_MONEY_PLACES)
        except ValueError:
            flash('Mintos evolution requires a valid total value.', 'danger')
            return redirect(url_for('investments'))
        if computed_total_value <= 0:
            flash('Mintos total value must be greater than zero.', 'danger')
            return redirect(url_for('investments'))
        apply_mintos_evolution(db, current_user.id, holding, valuation_date, computed_total_value, note)
        db.commit()
        flash('Evolution recorded and returns auto-computed.', 'success')
        return redirect(url_for('investments'))

    if holding_class == 'Fixed Income Fund':
        if quantity <= 0:
            flash('Holding quantity must be greater than zero to apply fixed income yield.', 'danger')
            return redirect(url_for('investments'))
        try:
            computed_return_pct = parse_decimal_field(return_pct_raw, 'Return %', DECIMAL_MONEY_PLACES)
        except ValueError:
            flash('Fixed income evolution requires a valid return %.', 'danger')
            return redirect(url_for('investments'))

        computed_return_amount = ((base_value * computed_return_pct) / Decimal('100')).quantize(DECIMAL_MONEY_PLACES)
        computed_total_value = (base_value + computed_return_amount).quantize(DECIMAL_MONEY_PLACES)
        if computed_total_value <= 0:
            flash('Computed total value must stay positive.', 'danger')
            return redirect(url_for('investments'))

    else:
        flash('Evolution automation is currently available for Mintos Lending and Fixed Income Fund holdings.', 'warning')
        return redirect(url_for('investments'))

    # Mintos Lending is handled earlier via apply_mintos_evolution; only the
    # per-share classes (Fixed Income Fund) reach this point.
    new_price = (computed_total_value / quantity).quantize(DECIMAL_MONEY_PLACES)
    quantity_to_store = quantity

    db.execute(
        '''INSERT INTO investment_valuations
           (user_id, holding_id, valuation_date, total_value, currency, return_pct, return_amount, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (
            current_user.id,
            holding['id'],
            valuation_date,
            float(computed_total_value),
            holding['currency'],
            float(computed_return_pct) if computed_return_pct is not None else None,
            float(computed_return_amount) if computed_return_amount is not None else None,
            note,
        ),
    )

    db.execute(
        '''UPDATE investment_holdings
           SET quantity = ?, current_price = ?, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND user_id = ?''',
        (float(quantity_to_store), float(new_price), holding['id'], current_user.id),
    )

    db.commit()
    flash('Evolution recorded and returns auto-computed.', 'success')
    return redirect(url_for('investments'))


# Transaction routes
TRANSACTIONS_PER_PAGE = 100


@app.route('/transactions')
@login_required
def transactions():
    db = get_db()

    # Get filter parameters
    account_id = request.args.get('account_id', type=int)
    category_id = request.args.get('category_id', type=int)
    trans_type = request.args.get('type')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    search = request.args.get('search', '').strip()

    query = '''
         SELECT t.*, a.name as account_name, a.currency as account_currency,
             c.name as category_name, c.icon as category_icon
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        LEFT JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ?
    '''
    params = [current_user.id]

    if account_id:
        query += ' AND t.account_id = ?'
        params.append(account_id)
    if category_id:
        query += ' AND t.category_id = ?'
        params.append(category_id)
    if trans_type:
        query += ' AND t.transaction_type = ?'
        params.append(trans_type)
    if start_date:
        query += ' AND t.date >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND t.date <= ?'
        params.append(end_date)
    if search:
        query += ' AND (t.description LIKE ? OR t.payee LIKE ?)'
        params.extend([f'%{search}%', f'%{search}%'])

    total_count = db.execute(f'SELECT COUNT(*) FROM ({query})', params).fetchone()[0]
    page_count = max(1, -(-total_count // TRANSACTIONS_PER_PAGE))
    page = min(max(request.args.get('page', 1, type=int), 1), page_count)

    query += ' ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?'
    params.extend([TRANSACTIONS_PER_PAGE, (page - 1) * TRANSACTIONS_PER_PAGE])

    transactions = db.execute(query, params).fetchall()
    accounts = db.execute('SELECT * FROM accounts WHERE user_id = ? ORDER BY name',
                         (current_user.id,)).fetchall()
    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY name',
                           (current_user.id,)).fetchall()

    return render_template('transactions.html',
                         transactions=transactions,
                         accounts=accounts,
                         categories=categories,
                         page=page,
                         page_count=page_count,
                         total_count=total_count,
                         filters={
                             'account_id': account_id,
                             'category_id': category_id,
                             'type': trans_type,
                             'start_date': start_date,
                             'end_date': end_date,
                             'search': search
                         })


@app.route('/transactions/add', methods=['GET', 'POST'])
@login_required
def add_transaction():
    db = get_db()

    if request.method == 'POST':
        account_id = request.form['account_id']
        category_id = request.form.get('category_id') or None
        trans_type = request.form['transaction_type']
        amount = form_amount('amount', 'Amount')
        description = request.form.get('description', '').strip()
        payee = request.form.get('payee', '').strip()
        date = request.form['date']

        db.execute(
            '''INSERT INTO transactions (user_id, account_id, category_id, transaction_type, amount, description, payee, date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (current_user.id, account_id, category_id, trans_type, amount, description, payee, date)
        )
        db.commit()
        flash('Transaction added successfully!', 'success')
        return redirect(url_for('transactions'))

    accounts = db.execute('SELECT * FROM accounts WHERE user_id = ? ORDER BY name',
                         (current_user.id,)).fetchall()
    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()

    return render_template('transaction_form.html',
                         transaction=None,
                         accounts=accounts,
                         categories=categories,
                         today=datetime.now().strftime('%Y-%m-%d'))


@app.route('/transactions/<int:transaction_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_transaction(transaction_id):
    db = get_db()
    transaction = db.execute('SELECT * FROM transactions WHERE id = ? AND user_id = ?',
                            (transaction_id, current_user.id)).fetchone()

    if not transaction:
        flash('Transaction not found.', 'danger')
        return redirect(url_for('transactions'))

    if request.method == 'POST':
        account_id = request.form['account_id']
        category_id = request.form.get('category_id') or None
        trans_type = request.form['transaction_type']
        amount = form_amount('amount', 'Amount')
        description = request.form.get('description', '').strip()
        payee = request.form.get('payee', '').strip()
        date = request.form['date']

        db.execute(
            '''UPDATE transactions SET account_id = ?, category_id = ?, transaction_type = ?,
               amount = ?, description = ?, payee = ?, date = ? WHERE id = ? AND user_id = ?''',
            (account_id, category_id, trans_type, amount, description, payee, date, transaction_id, current_user.id)
        )
        db.commit()
        flash('Transaction updated successfully!', 'success')
        return redirect(url_for('transactions'))

    accounts = db.execute('SELECT * FROM accounts WHERE user_id = ? ORDER BY name',
                         (current_user.id,)).fetchall()
    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()

    return render_template('transaction_form.html',
                         transaction=transaction,
                         accounts=accounts,
                         categories=categories,
                         today=datetime.now().strftime('%Y-%m-%d'))


@app.route('/transactions/<int:transaction_id>/delete', methods=['POST'])
@login_required
def delete_transaction(transaction_id):
    db = get_db()
    db.execute('DELETE FROM transactions WHERE id = ? AND user_id = ?',
               (transaction_id, current_user.id))
    db.commit()
    flash('Transaction deleted.', 'info')
    return redirect(url_for('transactions'))


# Import routes
# ---------------------------------------------------------------------------
# Bank import: raw exports dropped in data/raw/<source>/ are parsed, previewed
# and ingested as transactions (and investment operations for Trade Republic).
# ---------------------------------------------------------------------------

def parse_bank_export_date(value):
    """Return YYYY-MM-DD from a raw export timestamp, or '' when unparseable."""
    text = (value or '').strip()
    if len(text) >= 10:
        candidate = text[:10]
        try:
            datetime.strptime(candidate, '%Y-%m-%d')
            return candidate
        except ValueError:
            pass
    return ''


def parse_revolut_csv(csv_text):
    """Parse a raw Revolut export (all currencies/sub-accounts) into normalized rows."""
    # Revolut exports are always comma-delimited; descriptions may contain ';',
    # so the generic delimiter sniffing helpers must not be used here.
    reader = csv.DictReader(io.StringIO(csv_text))

    skipped = defaultdict(int)
    completed_rows = []
    for row in reader:
        if not (row.get('id') or '').strip():
            continue
        state = (row.get('state') or '').strip().upper()
        if state and state != 'COMPLETED':
            skipped[state] += 1
            continue
        completed_rows.append(row)

    # Same-currency TRANSFER pairs (vault moves) share one Revolut id: both legs
    # are internal and should be typed 'transfer'.
    pair_counts = defaultdict(int)
    for row in completed_rows:
        if (row.get('type') or '').strip().upper() == 'TRANSFER':
            pair_counts[((row.get('id') or '').strip(), normalize_currency(row.get('currency')))] += 1

    rows = []
    for row in completed_rows:
        raw_id = (row.get('id') or '').strip()
        leg_id = (row.get('legId') or '').strip() or raw_id

        date = (
            parse_bank_export_date(row.get('completedDate'))
            or parse_bank_export_date(row.get('startedDate'))
            or parse_bank_export_date(row.get('createdDate'))
        )
        if not date:
            skipped['NO_DATE'] += 1
            continue

        # Amounts are in minor units; amountWithCharges already nets out fees.
        raw_amount = (row.get('amountWithCharges') or '').strip() or (row.get('amount') or '').strip() or '0'
        try:
            amount = (Decimal(raw_amount) / Decimal('100')).quantize(Decimal('0.01'))
        except InvalidOperation:
            skipped['BAD_AMOUNT'] += 1
            continue

        row_type = (row.get('type') or '').strip().upper()
        currency = normalize_currency(row.get('currency'))
        if row_type == 'EXCHANGE' or (row_type == 'TRANSFER' and pair_counts.get((raw_id, currency), 0) >= 2):
            transaction_type = 'transfer'
        else:
            transaction_type = 'income' if amount >= 0 else 'expense'

        description = (row.get('description') or '').strip()
        comment = (row.get('comment') or '').strip()
        reason = (row.get('reason') or '').strip()
        memo_parts = [description] if description else []
        if comment and comment != description:
            memo_parts.append(comment)
        if reason:
            memo_parts.append(f'[{reason}]')

        rows.append({
            # legId (unique per leg) — using id would collapse exchange/vault pairs.
            'id_transaction': f'rev_{leg_id}',
            # Raw Revolut id: matches transactions imported by the old converter script.
            'legacy_id': raw_id or None,
            'date': date,
            'amount': amount,
            'transaction_type': transaction_type,
            'currency': currency,
            'category_text': (row.get('category') or '').strip() or (row.get('merchant_category') or '').strip(),
            'description': ' | '.join(memo_parts),
            'payee': (row.get('merchant_name') or '').strip(),
        })

    return {'rows': rows, 'investment_rows': [], 'skipped': dict(skipped)}


def parse_trade_republic_csv(csv_text):
    """Parse a Trade Republic export into cash rows plus investment buy rows."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    investment_rows = []
    skipped = defaultdict(int)

    for row in reader:
        raw_id = (row.get('id_transaction') or '').strip()
        date = parse_bank_export_date(row.get('date')) or parse_bank_export_date(row.get('datetime'))
        if not raw_id or not date:
            skipped['NO_ID_OR_DATE'] += 1
            continue

        try:
            amount = Decimal((row.get('amount') or '').strip() or '0').quantize(Decimal('0.01'))
        except InvalidOperation:
            skipped['BAD_AMOUNT'] += 1
            continue

        row_type = (row.get('type') or '').strip().upper()
        if row_type in TRADE_REPUBLIC_BUY_TYPES:
            # Cash leg of a security purchase: typed transfer so it stays out of
            # income/expense reports (the holding tracks the value instead).
            transaction_type = 'transfer'
        elif row_type in TRADE_REPUBLIC_INCOME_TYPES:
            transaction_type = 'income'
        else:
            transaction_type = 'income' if amount >= 0 else 'expense'

        id_transaction = f'tr_{raw_id}'
        rows.append({
            'id_transaction': id_transaction,
            'legacy_id': None,
            'date': date,
            'amount': amount,
            'transaction_type': transaction_type,
            'currency': normalize_currency(row.get('currency')),
            'category_text': (row.get('category') or '').strip(),
            'description': (row.get('description') or '').strip(),
            'payee': (row.get('counterparty_name') or '').strip() or (row.get('name') or '').strip(),
        })

        # Only settled BUY rows (shares + price) become investment operations.
        # PRIVATE_MARKET_BUY rows are cash pre-payments that are settled later by
        # a BUY row for the same ISIN — creating units from them would double-count.
        if (
            row_type == 'BUY'
            and (row.get('symbol') or '').strip()
            and (row.get('shares') or '').strip()
            and (row.get('price') or '').strip()
        ):
            investment_rows.append({
                'id_transaction': id_transaction,
                'date': date,
                'operation': row_type,
                'asset_class': (row.get('asset_class') or '').strip().upper(),
                'name': (row.get('name') or '').strip(),
                'symbol': normalize_isin(row.get('symbol')),
                'shares': (row.get('shares') or '').strip(),
                'price': (row.get('price') or '').strip(),
                'amount': abs(amount),
                'currency': normalize_currency(row.get('currency')),
            })

    return {'rows': rows, 'investment_rows': investment_rows, 'skipped': dict(skipped)}


def synthetic_bank_id(prefix, *parts):
    """Stable id for exports without a transaction id: hash of the row content
    (including an occurrence index so identical rows stay distinct)."""
    base = '|'.join(str(part or '').strip() for part in parts)
    digest = hashlib.sha256(base.encode('utf-8')).hexdigest()[:16]
    return f'{prefix}_{digest}'


B100_INTERNAL_SAVE_RE = re.compile(r'off to save|move to save', re.IGNORECASE)


def parse_b100_csv(csv_text):
    """Parse a B100 export. Columns: Fecha de Operación, Fecha valor, Detalle,
    Concepto, Cantidad, Saldo tras operación, Divisa, Tipo de Movimiento."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    skipped = defaultdict(int)
    occurrences = defaultdict(int)

    for row in reader:
        try:
            date = parse_import_date((row.get('Fecha de Operación') or '').strip())
        except ValueError:
            skipped['NO_DATE'] += 1
            continue
        try:
            amount = Decimal((row.get('Cantidad') or '').strip() or '0').quantize(Decimal('0.01'))
        except InvalidOperation:
            skipped['BAD_AMOUNT'] += 1
            continue

        detalle = (row.get('Detalle') or '').strip()
        concepto = (row.get('Concepto') or '').strip()
        saldo = (row.get('Saldo tras operación') or '').strip()

        # B100's automatic-savings moves are internal: keep them out of
        # income/expense reports (same decision as Revolut vault transfers).
        if B100_INTERNAL_SAVE_RE.search(f'{detalle} {concepto}'):
            transaction_type = 'transfer'
        else:
            transaction_type = 'income' if amount >= 0 else 'expense'

        key = (date, str(amount), detalle, concepto, saldo)
        occurrences[key] += 1

        rows.append({
            'id_transaction': synthetic_bank_id('b100', *key, occurrences[key]),
            'legacy_id': None,
            'date': date,
            'amount': amount,
            'transaction_type': transaction_type,
            'currency': normalize_currency(row.get('Divisa')),
            # Detalle text ('Transferencia enviada', ...) must NOT go into
            # category_text: is_transfer_like_category would force everything
            # to transfer and zero the account balance.
            'category_text': '',
            'description': ' | '.join(part for part in (detalle, concepto) if part),
            'payee': concepto,
        })

    return {'rows': rows, 'investment_rows': [], 'skipped': dict(skipped)}


def load_xlsx_worksheet(path):
    try:
        openpyxl = importlib.import_module('openpyxl')
    except ImportError as exc:
        raise ValueError('openpyxl is required for XLSX bank imports') from exc
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        # Not read_only: some statements report broken sheet dimensions there.
        workbook = openpyxl.load_workbook(path, data_only=True)
    return workbook[workbook.sheetnames[0]]


def _edenred_format_a_min_date(folder, product_token):
    """Earliest date covered by any Format-A ('Movimientos') file for this product.
    Format-B rows from on/after this date are skipped (Format A covers them with
    merchant text); older Format-B rows backfill history A doesn't reach."""
    min_date = None
    if not os.path.isdir(folder):
        return None
    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith('.xlsx'):
            continue
        path = os.path.join(folder, name)
        if _edenred_file_product(path) != product_token:
            continue
        try:
            worksheet = load_xlsx_worksheet(path)
        except Exception:
            continue
        rows = list(worksheet.iter_rows(values_only=True))
        if _edenred_is_format_b(rows):
            continue  # Format-B file, not Format A
        for date in _edenred_format_a_dates(rows):
            if min_date is None or date < min_date:
                min_date = date
    return min_date


def _edenred_format_a_dates(rows):
    in_table = False
    for raw_row in rows:
        cells = list(raw_row) + [None] * 3
        if not in_table:
            if str(cells[0] or '').strip() == 'Fecha':
                in_table = True
            continue
        raw_date = cells[0]
        if raw_date is None:
            continue
        timestamp = raw_date.strftime('%Y-%m-%d %H:%M:%S') if isinstance(raw_date, datetime) else str(raw_date).strip()
        date = parse_bank_export_date(timestamp)
        if date:
            yield date


def _edenred_file_product(path):
    """Which Edenred product a file belongs to, decided by its filename
    ('...Movilidad...' -> movilidad, '...Restaurante...' -> restaurant). The
    filename is authoritative for routing each export to the right account."""
    name = os.path.basename(path).lower()
    if 'movilidad' in name:
        return 'movilidad'
    if 'restaurante' in name:
        return 'restaurant'
    return None


def _edenred_is_format_b(rows):
    """True for the plain 'Sheet1' export (signed amounts, no merchant text) vs
    the 'Movimientos' export (Format A). Layout family only, product-agnostic."""
    if not rows:
        return False
    header = [str(c or '') for c in (list(rows[0]) + [None] * 5)[:5]]
    return 'Num. Tarjeta' in header[0] or 'Tipo de Operaci' in header[0]


def parse_edenred_xlsx(path, product_token):
    """Parse an Edenred movements export. Files are routed to a product by their
    filename; two layouts exist: Format A ('Movimientos' sheet, rich merchant
    text) and Format B ('Sheet1', signed amounts, no merchant, longer history)."""
    if _edenred_file_product(path) != product_token:
        return {'rows': [], 'investment_rows': [], 'skipped': {}}
    worksheet = load_xlsx_worksheet(path)
    rows = list(worksheet.iter_rows(values_only=True))
    if _edenred_is_format_b(rows):
        return _parse_edenred_format_b(path, rows, product_token)
    return _parse_edenred_format_a(rows, product_token)


def _parse_edenred_format_a(rows, product_token):
    """Format A ('Movimientos'): the table starts at a 'Fecha' header row; rows
    carry rich merchant text. The file was already routed by filename."""
    out = []
    skipped = defaultdict(int)
    occurrences = defaultdict(int)

    in_table = False
    for raw_row in rows:
        cells = list(raw_row) + [None] * 3
        first = str(cells[0] or '').strip()
        if not in_table:
            if first == 'Fecha':
                in_table = True
            continue

        raw_date, detail, importe = cells[0], cells[1], cells[2]
        if raw_date is None:
            continue
        timestamp = raw_date.strftime('%Y-%m-%d %H:%M:%S') if isinstance(raw_date, datetime) else str(raw_date).strip()
        date = parse_bank_export_date(timestamp)
        if not date:
            skipped['NO_DATE'] += 1
            continue
        try:
            amount = Decimal(str(importe if importe is not None else '0')).quantize(Decimal('0.01'))
        except InvalidOperation:
            skipped['BAD_AMOUNT'] += 1
            continue

        detail = str(detail or '').strip()
        # Edenred amounts are charges: RECARGA rows are top-ups, the rest is
        # spending (positive) or a refund (negative charge -> money back in).
        is_topup = detail.strip().upper() == 'RECARGA'
        if not is_topup:
            amount = -amount
        transaction_type = 'income' if amount >= 0 else 'expense'

        key = (product_token, timestamp, detail, str(abs(amount)))
        occurrences[key] += 1

        out.append({
            'id_transaction': synthetic_bank_id('edr', *key, occurrences[key]),
            'legacy_id': None,
            'date': date,
            'amount': amount,
            'transaction_type': transaction_type,
            'currency': 'EUR',
            'category_text': '',
            'description': detail,
            'payee': '' if is_topup else detail.split(' - ')[0].strip(),
        })

    return {'rows': out, 'investment_rows': [], 'skipped': dict(skipped)}


def _parse_edenred_format_b(path, rows, product_token):
    """Format B ('Sheet1'): signed amounts, no merchant text. Used to backfill the
    older history Format A doesn't reach; rows already covered by a Format-A file
    (date >= that file's earliest date) are skipped so the richer A row wins."""
    # Column layout depends on the product (the file was routed by filename).
    # Mobility B: type(0) | monto(1) | fecha(2).  Restaurant B has the amount and
    # date shifted right (num/usuario prefix columns).
    if product_token == 'restaurant':
        type_idx, date_idx, amount_idx = 2, 3, 4
    else:
        type_idx, date_idx, amount_idx = 0, 2, 1

    cutoff = _edenred_format_a_min_date(os.path.dirname(path), product_token)

    out = []
    skipped = defaultdict(int)
    occurrences = defaultdict(int)
    for raw_row in rows[1:]:
        cells = list(raw_row) + [None] * 5
        raw_date, monto = cells[date_idx], cells[amount_idx]
        if raw_date is None or monto is None:
            continue
        timestamp = raw_date.strftime('%Y-%m-%d %H:%M:%S') if isinstance(raw_date, datetime) else str(raw_date).strip()
        date = parse_bank_export_date(timestamp)
        if not date:
            skipped['NO_DATE'] += 1
            continue
        if cutoff and date >= cutoff:
            skipped['COVERED_BY_FORMAT_A'] += 1
            continue
        try:
            amount = Decimal(str(monto)).quantize(Decimal('0.01'))
        except InvalidOperation:
            skipped['BAD_AMOUNT'] += 1
            continue

        op_type = str(cells[type_idx] or '').strip()
        transaction_type = 'income' if amount >= 0 else 'expense'

        key = (product_token, date, str(abs(amount)))
        occurrences[key] += 1

        out.append({
            'id_transaction': synthetic_bank_id('edr', *key, occurrences[key]),
            'legacy_id': None,
            'date': date,
            'amount': amount,
            'transaction_type': transaction_type,
            'currency': 'EUR',
            'category_text': '',
            'description': op_type,
            'payee': '',
        })

    return {'rows': out, 'investment_rows': [], 'skipped': dict(skipped)}


def parse_iberia_cards_xlsx(path):
    """Parse an Iberia Cards monthly statement: transaction tables follow a
    'N° / FECHA OPERACIÓN / COMERCIO / IMPORTE DIVISA / IMPORTE EUROS' header
    (one table per card)."""
    worksheet = load_xlsx_worksheet(path)
    rows = []
    skipped = defaultdict(int)
    occurrences = defaultdict(int)

    in_table = False
    for raw_row in worksheet.iter_rows(values_only=True):
        cells = list(raw_row) + [None] * 9
        fecha, comercio, importe_eur = cells[2], cells[3], cells[5]

        if isinstance(fecha, str) and 'FECHA OPERACI' in fecha.upper():
            in_table = True
            continue
        if not in_table:
            continue

        if isinstance(fecha, datetime):
            date = fecha.strftime('%Y-%m-%d')
        else:
            try:
                date = parse_import_date(str(fecha or '').strip())
            except ValueError:
                # Dateless row with COMERCIO text = summary block ('Deuda
                # pendiente...', 'TOTAL A CARGAR') ends the table; blank spacer
                # rows are tolerated.
                if str(comercio or '').strip():
                    in_table = False
                continue

        if not isinstance(importe_eur, (int, float)):
            skipped['BAD_AMOUNT'] += 1
            continue
        comercio = str(comercio or '').strip()
        # Statement amounts are charges: positive = expense, negative = refund.
        amount = (-Decimal(str(importe_eur))).quantize(Decimal('0.01'))
        transaction_type = 'income' if amount >= 0 else 'expense'

        key = (date, comercio, str(abs(amount)))
        occurrences[key] += 1

        rows.append({
            'id_transaction': synthetic_bank_id('ibc', *key, occurrences[key]),
            'legacy_id': None,
            'date': date,
            'amount': amount,
            'transaction_type': transaction_type,
            'currency': 'EUR',
            'category_text': '',
            'description': comercio,
            'payee': comercio,
        })

    return {'rows': rows, 'investment_rows': [], 'skipped': dict(skipped)}


def parse_mintos_xlsx(path):
    """Aggregate a Mintos 'Investors Account Statement' export. Deposits are
    classified for cost basis; earnings/costs net into the gain; internal moves
    (cash<->loans) only shift allocation. Assumes a full-history export."""
    worksheet = load_xlsx_worksheet(path)
    rows = list(worksheet.iter_rows(values_only=True))
    skipped = defaultdict(int)
    result = {
        'currency': 'EUR',
        'statement_end_date': '',
        'date_from': '',
        'deposits': [],
        'total_deposits': Decimal('0'),
        'net_earnings': Decimal('0'),
        'cash_balance': Decimal('0'),
        'outstanding_principal': Decimal('0'),
        'current_total_value': Decimal('0'),
        'bucket_counts': {'external_deposit': 0, 'internal': 0, 'earning': 0, 'cost': 0},
        'skipped': {},
    }
    if not rows:
        return result

    header = [str(c or '').strip() for c in rows[0]]

    def col(name):
        return header.index(name) if name in header else None

    idx_date = col('Date')
    idx_type = col('Transaction Type')
    idx_amount = col('Amount')
    idx_currency = col('Currency')
    idx_from = col('Date from')
    if idx_date is None or idx_type is None or idx_amount is None:
        skipped['BAD_HEADER'] += 1
        result['skipped'] = dict(skipped)
        return result

    occurrences = defaultdict(int)
    currencies = set()
    max_date = ''
    internal_sum = Decimal('0')
    for raw_row in rows[1:]:
        cells = list(raw_row) + [None] * (len(header) + 1)
        raw_date, raw_type, raw_amount = cells[idx_date], cells[idx_type], cells[idx_amount]
        if raw_type is None and raw_amount is None:
            continue
        timestamp = raw_date.strftime('%Y-%m-%d %H:%M:%S') if isinstance(raw_date, datetime) else str(raw_date or '').strip()
        date = parse_bank_export_date(timestamp)
        if not date:
            skipped['NO_DATE'] += 1
            continue
        try:
            amount = Decimal(str(raw_amount if raw_amount is not None else '0')).quantize(Decimal('0.01'))
        except InvalidOperation:
            skipped['BAD_AMOUNT'] += 1
            continue

        op_type = str(raw_type or '').strip()
        op_key = op_type.lower()
        if idx_currency is not None and cells[idx_currency]:
            currencies.add(str(cells[idx_currency]).strip().upper())
        if date > max_date:
            max_date = date

        if op_key in MINTOS_DEPOSIT_TYPES:
            result['bucket_counts']['external_deposit'] += 1
            occ_key = (date, op_key, str(amount))
            occurrences[occ_key] += 1
            result['deposits'].append({
                'id_transaction': synthetic_bank_id('mintos', date, op_key, str(amount), occurrences[occ_key]),
                'date': date,
                'amount': amount,
                'type': op_type,
            })
            result['total_deposits'] += amount
        elif op_key in MINTOS_INTERNAL_TYPES:
            result['bucket_counts']['internal'] += 1
            internal_sum += amount
        elif op_key in MINTOS_EARNING_TYPES:
            result['bucket_counts']['earning'] += 1
            result['net_earnings'] += amount
        elif op_key in MINTOS_COST_TYPES:
            result['bucket_counts']['cost'] += 1
            result['net_earnings'] += amount  # costs are already negative
        else:
            skipped[f'UNKNOWN_TYPE:{op_type}'] += 1
            continue

        result['cash_balance'] += amount

    result['statement_end_date'] = max_date
    if idx_from is not None and len(rows) > 1:
        first_from = rows[1][idx_from]
        result['date_from'] = first_from.strftime('%Y-%m-%d') if isinstance(first_from, datetime) else str(first_from or '')[:10]
    result['outstanding_principal'] = (-internal_sum).quantize(Decimal('0.01'))
    result['current_total_value'] = (result['total_deposits'] + result['net_earnings']).quantize(Decimal('0.01'))
    if len(currencies) == 1:
        result['currency'] = currencies.pop()
    elif len(currencies) > 1:
        skipped['MIXED_CURRENCY'] += 1
    result['skipped'] = dict(skipped)
    return result


def find_or_create_mintos_holding(db, user_id, account_id, create_if_missing, currency='EUR'):
    """Return the Mintos Lending holding in an investment account (creating it
    when asked). When several exist the caller must pass an explicit holding_id."""
    holding = db.execute(
        '''SELECT * FROM investment_holdings
           WHERE user_id = ? AND account_id = ? AND asset_class = 'Mintos Lending'
           ORDER BY id''',
        (user_id, account_id),
    ).fetchone()
    if holding or not create_if_missing:
        return holding
    cursor = db.execute(
        '''INSERT INTO investment_holdings
           (user_id, account_id, asset_name, ticker, isin, asset_class, currency,
            quantity, avg_cost, current_price, updated_at)
           VALUES (?, ?, 'Mintos', '', '', 'Mintos Lending', ?, 1, 0, 0, CURRENT_TIMESTAMP)''',
        (user_id, account_id, currency),
    )
    return db.execute(
        'SELECT * FROM investment_holdings WHERE id = ?', (cursor.lastrowid,)
    ).fetchone()


def list_mintos_files():
    if not os.path.isdir(MINTOS_FOLDER):
        return []
    return sorted(
        name for name in os.listdir(MINTOS_FOLDER)
        if name.lower().endswith(MINTOS_EXTENSIONS) and os.path.isfile(os.path.join(MINTOS_FOLDER, name))
    )


def build_mintos_import_plan(db, user_id, account_id, holding_id, create_holding):
    """Parse and merge every Mintos export, resolve the target holding, and work
    out the deposits to add and the total value to book. Shared by preview and
    confirm. Assumes each file is a full-history export (Date from ~ inception)."""
    files = list_mintos_files()
    merged_deposits = {}
    skipped = defaultdict(int)
    file_summaries = []
    latest = None
    earliest_from = None

    for name in files:
        path = os.path.join(MINTOS_FOLDER, name)
        try:
            parsed = parse_mintos_xlsx(path)
        except Exception as exc:
            raise ValueError(f'Could not read {name}: {exc}')
        for dep in parsed['deposits']:
            merged_deposits[dep['id_transaction']] = dep
        for state, count in parsed['skipped'].items():
            skipped[state] += count
        file_summaries.append({
            'name': name,
            'row_count': sum(parsed['bucket_counts'].values()),
            'deposits': len(parsed['deposits']),
        })
        # The statement reaching the latest date is authoritative (file names are
        # DD_MM_YYYY, so name order is not date order).
        if latest is None or parsed['statement_end_date'] >= latest['statement_end_date']:
            latest = parsed
        if parsed['date_from'] and (earliest_from is None or parsed['date_from'] < earliest_from):
            earliest_from = parsed['date_from']

    # Resolve the existing holding (without creating) for preview math.
    holding = None
    if holding_id:
        holding = db.execute(
            '''SELECT * FROM investment_holdings
               WHERE id = ? AND user_id = ? AND account_id = ? AND asset_class = 'Mintos Lending' ''',
            (holding_id, user_id, account_id),
        ).fetchone()
    elif account_id:
        holding = find_or_create_mintos_holding(db, user_id, account_id, create_if_missing=False)

    existing_op_ids = {
        row['id_transaction'] for row in db.execute(
            'SELECT id_transaction FROM investment_operations WHERE user_id = ? AND id_transaction IS NOT NULL',
            (user_id,),
        ).fetchall()
    }
    deposits = sorted(merged_deposits.values(), key=lambda d: (d['date'], d['id_transaction']))
    new_deposits = [d for d in deposits if d['id_transaction'] not in existing_op_ids]
    new_deposit_total = sum((d['amount'] for d in new_deposits), Decimal('0')).quantize(DECIMAL_MONEY_PLACES)

    current_total_value = latest['current_total_value'] if latest else Decimal('0')
    statement_end_date = latest['statement_end_date'] if latest else ''
    net_earnings = latest['net_earnings'] if latest else Decimal('0')

    base_value = get_holding_base_value(db, user_id, holding, statement_end_date) if holding else Decimal('0')
    projected_base = (base_value + new_deposit_total).quantize(DECIMAL_MONEY_PLACES)
    gain_to_book = (current_total_value - projected_base).quantize(DECIMAL_MONEY_PLACES) if current_total_value else Decimal('0')

    # A windowed (non-full-history) export would understate deposits/earnings.
    partial_warning = bool(latest and earliest_from and latest['date_from'] and latest['date_from'] > earliest_from)

    return {
        'files': file_summaries,
        'currency': latest['currency'] if latest else DEFAULT_BASE_CURRENCY,
        'statement_end_date': statement_end_date,
        'date_from': latest['date_from'] if latest else '',
        'deposits': deposits,
        'total_deposits': latest['total_deposits'] if latest else Decimal('0'),
        'new_deposit_count': len(new_deposits),
        'new_deposit_total': new_deposit_total,
        'existing_deposit_count': len(deposits) - len(new_deposits),
        'net_earnings': net_earnings,
        'current_total_value': current_total_value,
        'gain_to_book': gain_to_book,
        'bucket_counts': latest['bucket_counts'] if latest else {},
        'skipped': dict(skipped),
        'holding': holding,
        'partial_warning': partial_warning,
    }


def import_mintos_statement(db, user_id, holding, plan):
    """Apply new deposits to the cost basis, then set the current total value via
    an evolution valuation. Idempotent: deposits are keyed by id_transaction and
    a statement already booked at the same date and value is left unchanged."""
    existing_ids = {
        row['id_transaction'] for row in db.execute(
            'SELECT id_transaction FROM investment_operations WHERE user_id = ? AND id_transaction IS NOT NULL',
            (user_id,),
        ).fetchall()
    }
    invested = Decimal(str(holding['avg_cost'] or 0)).quantize(DECIMAL_MONEY_PLACES)
    value = Decimal(str(holding['current_price'] or 0)).quantize(DECIMAL_MONEY_PLACES)
    deposits_created = 0
    for dep in plan['deposits']:
        if dep['id_transaction'] in existing_ids:
            continue
        invested = (invested + dep['amount']).quantize(DECIMAL_MONEY_PLACES)
        value = (value + dep['amount']).quantize(DECIMAL_MONEY_PLACES)
        db.execute(
            '''UPDATE investment_holdings
               SET quantity = 1, avg_cost = ?, current_price = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?''',
            (float(invested), float(value), holding['id'], user_id),
        )
        db.execute(
            '''INSERT INTO investment_operations
               (user_id, account_id, holding_id, operation_type, amount, currency, is_reinvested, note, operation_date, id_transaction)
               VALUES (?, ?, ?, 'deposit', ?, ?, 0, ?, ?, ?)''',
            (
                user_id,
                holding['account_id'],
                holding['id'],
                float(dep['amount']),
                holding['currency'],
                'Imported from Mintos statement',
                dep['date'],
                dep['id_transaction'],
            ),
        )
        deposits_created += 1

    holding = db.execute('SELECT * FROM investment_holdings WHERE id = ?', (holding['id'],)).fetchone()

    valuation_action = 'none'
    target_value = plan['current_total_value']
    valuation_date = plan['statement_end_date']
    if target_value and valuation_date:
        existing_val = db.execute(
            'SELECT id, total_value FROM investment_valuations WHERE user_id = ? AND holding_id = ? AND valuation_date = ?',
            (user_id, holding['id'], valuation_date),
        ).fetchone()
        if existing_val is not None and Decimal(str(existing_val['total_value'])).quantize(DECIMAL_MONEY_PLACES) == target_value.quantize(DECIMAL_MONEY_PLACES):
            valuation_action = 'unchanged'
        else:
            if existing_val is not None:
                db.execute('DELETE FROM investment_valuations WHERE id = ?', (existing_val['id'],))
                valuation_action = 'updated'
            else:
                valuation_action = 'created'
            # Base = previous valuation + deposits since (including the ones just
            # added), computed after dropping a superseded same-date valuation.
            apply_mintos_evolution(
                db, user_id, holding, valuation_date, target_value,
                note='Imported from Mintos statement',
            )

    return {'deposits_created': deposits_created, 'valuation_action': valuation_action}


BANK_IMPORT_PARSERS = {
    'revolut': parse_revolut_csv,
    'trade_republic': parse_trade_republic_csv,
    'b100': parse_b100_csv,
    'edenred_restaurant': lambda path: parse_edenred_xlsx(path, 'restaurant'),
    'edenred_mobility': lambda path: parse_edenred_xlsx(path, 'movilidad'),
    'iberia_cards': parse_iberia_cards_xlsx,
}


def list_bank_import_files(source):
    config = BANK_IMPORT_SOURCES[source]
    folder = config['folder']
    if not os.path.isdir(folder):
        return []
    return sorted(
        name for name in os.listdir(folder)
        if name.lower().endswith(config['extensions']) and os.path.isfile(os.path.join(folder, name))
    )


def load_bank_source_rows(source):
    """Parse and merge every file found for a source; newest file wins per id."""
    config = BANK_IMPORT_SOURCES[source]
    parser = BANK_IMPORT_PARSERS[source]

    merged_rows = {}
    merged_investments = {}
    skipped = defaultdict(int)
    file_summaries = []
    duplicate_count = 0

    # Sorted ascending: filenames carry the export date, so later files override
    # overlapping ids from earlier exports.
    for name in list_bank_import_files(source):
        path = os.path.join(config['folder'], name)
        try:
            if name.lower().endswith('.xlsx'):
                parsed = parser(path)
            else:
                with open(path, encoding='utf-8-sig', newline='') as handle:
                    parsed = parser(handle.read())
        except Exception as exc:
            raise ValueError(f'Could not read {name}: {exc}')

        for state, count in parsed['skipped'].items():
            skipped[state] += count
        for row in parsed['rows']:
            if row['id_transaction'] in merged_rows:
                duplicate_count += 1
            merged_rows[row['id_transaction']] = row
        for inv_row in parsed['investment_rows']:
            merged_investments[inv_row['id_transaction']] = inv_row

        file_summaries.append({'name': name, 'row_count': len(parsed['rows'])})

    return {
        'files': file_summaries,
        'rows': sorted(merged_rows.values(), key=lambda row: (row['date'], row['id_transaction'])),
        'investment_rows': sorted(merged_investments.values(), key=lambda row: (row['date'], row['id_transaction'])),
        'skipped': dict(skipped),
        'cross_file_duplicates': duplicate_count,
    }


def build_bank_import_plan(db, user_id, source, settings):
    """Load all source files, apply settings, and return everything the preview
    shows and the confirm step executes (single shared code path)."""
    config = BANK_IMPORT_SOURCES[source]
    data = load_bank_source_rows(source)
    accounts = {
        account['id']: account
        for account in db.execute('SELECT * FROM accounts WHERE user_id = ?', (user_id,)).fetchall()
    }

    from_date = (settings.get('from_date') or '').strip()
    currency_accounts = settings.get('currency_accounts') or {}
    enabled_currencies = set(settings.get('enabled_currencies') or [])
    transfer_patterns = [
        pattern.lower() for pattern in (settings.get('transfer_patterns') or []) if pattern
    ]

    prepared = []
    currency_stats = {}
    skipped_unmapped = 0
    skipped_before_date = 0
    pattern_transfers = 0

    for row in data['rows']:
        # User-configured patterns (e.g. the Iberia Cards bill debit) force
        # transfer type so the same money isn't counted as expense twice.
        pattern_forced = False
        if row['transaction_type'] != 'transfer' and transfer_patterns:
            row_text = f"{row['description']} {row['payee']}".lower()
            if any(pattern in row_text for pattern in transfer_patterns):
                row = {**row, 'transaction_type': 'transfer'}
                pattern_forced = True
        currency = row['currency']
        stats = currency_stats.setdefault(currency, {
            'currency': currency,
            'count': 0,
            'kept': 0,
            'income': Decimal('0'),
            'expense': Decimal('0'),
            'transfers': 0,
            'account_id': None,
            'account_name': None,
            'enabled': False,
            'currency_mismatch': False,
        })
        stats['count'] += 1

        if config['multi_currency']:
            account_id = currency_accounts.get(currency)
            enabled = currency in enabled_currencies
        else:
            account_id = settings.get('account_id')
            enabled = True

        account = accounts.get(account_id) if account_id else None
        if account:
            stats['account_id'] = account['id']
            stats['account_name'] = account['name']
            stats['currency_mismatch'] = normalize_currency(account['currency']) != currency
        stats['enabled'] = enabled

        if not account or not enabled:
            skipped_unmapped += 1
            continue
        if from_date and row['date'] < from_date:
            skipped_before_date += 1
            continue

        stats['kept'] += 1
        if row['transaction_type'] == 'income':
            stats['income'] += row['amount']
        elif row['transaction_type'] == 'expense':
            stats['expense'] += row['amount']
        else:
            stats['transfers'] += 1
            if pattern_forced:
                pattern_transfers += 1

        prepared_row = {**row, 'account_id': account['id']}
        # Source-level default category (e.g. Edenred food/transport) applied to
        # spending rows that the parser left uncategorised; a matching user rule
        # in upsert_bank_transactions still takes precedence.
        if config.get('default_category') and not prepared_row['category_text'] and row['transaction_type'] == 'expense':
            prepared_row['category_text'] = config['default_category']
        prepared.append(prepared_row)

    # Count old-script imports (raw Revolut id) that the confirm step will
    # migrate in place to the new legId-based ids.
    legacy_matches = 0
    if source == 'revolut' and prepared:
        mapped_account_ids = {row['account_id'] for row in prepared}
        placeholders = ','.join('?' for _ in mapped_account_ids)
        existing_ids = {
            row['id_transaction'] for row in db.execute(
                f'''SELECT id_transaction FROM transactions
                    WHERE user_id = ? AND account_id IN ({placeholders})
                      AND id_transaction IS NOT NULL''',
                (user_id, *mapped_account_ids),
            ).fetchall()
        }
        seen_legacy = set()
        for row in prepared:
            legacy_id = row.get('legacy_id')
            if (
                legacy_id
                and legacy_id not in seen_legacy
                and row['id_transaction'] not in existing_ids
                and legacy_id in existing_ids
            ):
                legacy_matches += 1
                seen_legacy.add(legacy_id)

    investment_plan = None
    if config['investments'] and settings.get('create_investments') and settings.get('investment_account_id'):
        investment_account_id = settings['investment_account_id']
        investment_rows = [
            row for row in data['investment_rows']
            if not from_date or row['date'] >= from_date
        ]
        existing_ops = {
            row['id_transaction'] for row in db.execute(
                '''SELECT id_transaction FROM investment_operations
                   WHERE user_id = ? AND id_transaction IS NOT NULL''',
                (user_id,),
            ).fetchall()
        }
        existing_isins = {
            normalize_isin(row['isin']) for row in db.execute(
                'SELECT isin FROM investment_holdings WHERE user_id = ? AND account_id = ?',
                (user_id, investment_account_id),
            ).fetchall() if row['isin']
        }
        new_rows = [row for row in investment_rows if row['id_transaction'] not in existing_ops]
        investment_account = accounts.get(investment_account_id)
        investment_plan = {
            'account_id': investment_account_id,
            'account_name': investment_account['name'] if investment_account else None,
            'rows': investment_rows,
            'new_count': len(new_rows),
            'existing_count': len(investment_rows) - len(new_rows),
            'new_holdings': sorted({
                f"{row['name'] or row['symbol']} ({row['symbol']})"
                for row in new_rows if row['symbol'] not in existing_isins
            }),
        }

    samples = list(reversed(prepared[-10:]))
    return {
        'source': source,
        'label': config['label'],
        'files': data['files'],
        'cross_file_duplicates': data['cross_file_duplicates'],
        'skipped_states': data['skipped'],
        'currencies': sorted(currency_stats.values(), key=lambda stats: -stats['count']),
        'rows': prepared,
        'skipped_unmapped': skipped_unmapped,
        'skipped_before_date': skipped_before_date,
        'pattern_transfers': pattern_transfers,
        'legacy_matches': legacy_matches,
        'investment_plan': investment_plan,
        'samples': samples,
    }


def upsert_bank_transactions(db, user_id, prepared_rows):
    """Insert/update normalized bank rows. Matches by id_transaction (user-wide,
    so remapping a currency to another account moves rows instead of duplicating
    them), falling back to the old converter script's raw Revolut id, which is
    migrated in place to the new id."""
    categories = db.execute(
        'SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
        (user_id,),
    ).fetchall()
    categories_by_id = {cat['id']: cat for cat in categories}
    rules = load_category_rules(db, user_id)

    counts = {'inserted': 0, 'updated': 0, 'migrated': 0}
    errors = []

    for row in prepared_rows:
        try:
            category_text = row['category_text']
            matched_category_id = match_import_category(
                user_id, rules, categories, row['description'], row['payee'], category_text
            )

            transaction_type = row['transaction_type']
            matched_category = categories_by_id.get(matched_category_id)
            # Transfer-like categories force transfer type, but never downgrade a
            # row the parser already marked as transfer.
            if transaction_type != 'transfer' and (
                (matched_category and matched_category['category_type'] == 'transfer')
                or is_transfer_like_category(category_text)
            ):
                transaction_type = 'transfer'

            outcome = upsert_transaction_by_external_id(
                db, user_id,
                {
                    'account_id': row['account_id'],
                    'category_id': matched_category_id,
                    'transaction_type': transaction_type,
                    'amount': float(abs(row['amount'])),
                    'description': row['description'],
                    'payee': row['payee'],
                    'date': row['date'],
                    'id_transaction': row['id_transaction'],
                },
                # User-wide match: remapping a currency to another account moves
                # rows instead of duplicating them; legacy ids migrate in place.
                [row['id_transaction'], row.get('legacy_id')],
            )
            counts[outcome] += 1
        except Exception as exc:
            errors.append(f"{row.get('id_transaction', '?')}: {exc}")

    return counts['inserted'], counts['updated'], counts['migrated'], errors


def import_tr_investment_ops(db, user_id, investment_account_id, investment_rows):
    """Create buy operations (and holdings as needed) from Trade Republic rows.
    Idempotent via investment_operations.id_transaction."""
    ops_created = 0
    ops_skipped = 0
    holdings_created = 0

    for row in investment_rows:
        existing = db.execute(
            'SELECT id FROM investment_operations WHERE user_id = ? AND id_transaction = ?',
            (user_id, row['id_transaction']),
        ).fetchone()
        if existing:
            ops_skipped += 1
            continue

        try:
            quantity = Decimal(row['shares']).quantize(DECIMAL_QUANTITY_PLACES)
            price = Decimal(row['price']).quantize(DECIMAL_MONEY_PLACES)
        except InvalidOperation:
            ops_skipped += 1
            continue
        if quantity <= 0 or price <= 0 or not row['symbol']:
            ops_skipped += 1
            continue
        amount = (quantity * price).quantize(DECIMAL_MONEY_PLACES)

        holding = db.execute(
            'SELECT * FROM investment_holdings WHERE user_id = ? AND account_id = ? AND isin = ?',
            (user_id, investment_account_id, row['symbol']),
        ).fetchone()
        if not holding:
            asset_class = TRADE_REPUBLIC_ASSET_CLASS_MAP.get(row['asset_class'], 'ETF')
            cursor = db.execute(
                '''INSERT INTO investment_holdings
                   (user_id, account_id, asset_name, ticker, isin, asset_class, currency,
                    quantity, avg_cost, current_price, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, CURRENT_TIMESTAMP)''',
                (
                    user_id,
                    investment_account_id,
                    row['name'] or row['symbol'],
                    '',
                    row['symbol'],
                    asset_class,
                    row['currency'],
                ),
            )
            holding = db.execute(
                'SELECT * FROM investment_holdings WHERE id = ?', (cursor.lastrowid,)
            ).fetchone()
            holdings_created += 1

        old_qty = Decimal(str(holding['quantity'] or 0)).quantize(DECIMAL_QUANTITY_PLACES)
        old_avg = Decimal(str(holding['avg_cost'] or 0)).quantize(DECIMAL_MONEY_PLACES)
        new_qty = (old_qty + quantity).quantize(DECIMAL_QUANTITY_PLACES)
        new_avg = (((old_qty * old_avg) + (quantity * price)) / new_qty).quantize(DECIMAL_MONEY_PLACES) if new_qty > 0 else Decimal('0')
        new_price = trade_price_or_current(db, holding, row['date'], price)
        db.execute(
            '''UPDATE investment_holdings
               SET quantity = ?, avg_cost = ?, current_price = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ?''',
            (float(new_qty), float(new_avg), float(new_price), holding['id'], user_id),
        )
        db.execute(
            '''INSERT INTO investment_operations
               (user_id, account_id, holding_id, operation_type, quantity, price_per_unit,
                amount, currency, is_reinvested, realized_gain, note, operation_date, id_transaction)
               VALUES (?, ?, ?, 'buy', ?, ?, ?, ?, 0, NULL, ?, ?, ?)''',
            (
                user_id,
                investment_account_id,
                holding['id'],
                float(quantity),
                float(price),
                float(amount),
                row['currency'],
                'Imported from Trade Republic',
                row['date'],
                row['id_transaction'],
            ),
        )
        ops_created += 1

    return ops_created, ops_skipped, holdings_created


def parse_bank_import_settings_from_form(source):
    form = request.form
    config = BANK_IMPORT_SOURCES[source]
    from_date = (form.get('from_date') or '').strip() or None
    transfer_patterns = [
        line.strip() for line in (form.get('transfer_patterns') or '').splitlines() if line.strip()
    ]
    if config['multi_currency']:
        currency_accounts = {}
        enabled_currencies = []
        for currency in form.getlist('currencies'):
            currency = normalize_currency(currency)
            if form.get(f'enabled_{currency}') == 'on':
                enabled_currencies.append(currency)
            account_id = form.get(f'account_{currency}', type=int)
            if account_id:
                currency_accounts[currency] = account_id
        return {
            'currency_accounts': currency_accounts,
            'enabled_currencies': enabled_currencies,
            'from_date': from_date,
            'transfer_patterns': transfer_patterns,
        }
    settings = {
        'account_id': form.get('account_id', type=int),
        'from_date': from_date,
        'transfer_patterns': transfer_patterns,
    }
    if config['investments']:
        settings['investment_account_id'] = form.get('investment_account_id', type=int)
        settings['create_investments'] = form.get('create_investments') == 'on'
    return settings


def validate_bank_import_settings(db, user_id, source, settings):
    """Normalize settings (JSON round-trips may stringify ids) and verify account
    ownership. Returns an error message or None."""
    config = BANK_IMPORT_SOURCES[source]
    account_ids = set()
    if config['multi_currency']:
        try:
            settings['currency_accounts'] = {
                normalize_currency(currency): int(account_id)
                for currency, account_id in (settings.get('currency_accounts') or {}).items()
                if account_id
            }
        except (TypeError, ValueError):
            return 'Invalid account mapping.'
        settings['enabled_currencies'] = [
            normalize_currency(currency) for currency in (settings.get('enabled_currencies') or [])
        ]
        if not any(
            currency in settings['currency_accounts']
            for currency in settings['enabled_currencies']
        ):
            return 'Map at least one enabled currency to an account.'
        account_ids.update(settings['currency_accounts'].values())
    else:
        try:
            settings['account_id'] = int(settings.get('account_id') or 0) or None
        except (TypeError, ValueError):
            return 'Invalid account selected.'
        if not settings['account_id']:
            return f"Select a target account for {config['label']}."
        account_ids.add(settings['account_id'])
        if config['investments']:
            try:
                settings['investment_account_id'] = int(settings.get('investment_account_id') or 0) or None
            except (TypeError, ValueError):
                return 'Invalid investment account selected.'
            settings['create_investments'] = bool(settings.get('create_investments'))
            if settings['create_investments']:
                if not settings['investment_account_id']:
                    return 'Select an investment account or disable investment creation.'
                valid_investment_ids = {acc['id'] for acc in get_investment_accounts(db, user_id)}
                if settings['investment_account_id'] not in valid_investment_ids:
                    return 'Invalid investment account selected.'
        else:
            settings['create_investments'] = False

    if account_ids:
        placeholders = ','.join('?' for _ in account_ids)
        owned = {
            row['id'] for row in db.execute(
                f'SELECT id FROM accounts WHERE user_id = ? AND id IN ({placeholders})',
                (user_id, *account_ids),
            ).fetchall()
        }
        if account_ids - owned:
            return 'Invalid account selected.'

    if settings.get('from_date'):
        try:
            settings['from_date'] = parse_import_date(settings['from_date'])
        except ValueError:
            return 'Invalid from date.'
    else:
        settings['from_date'] = None

    settings['transfer_patterns'] = [
        str(pattern).strip() for pattern in (settings.get('transfer_patterns') or [])
        if str(pattern).strip()
    ]
    return None


def build_bank_import_context(db, user_id):
    accounts = db.execute(
        'SELECT * FROM accounts WHERE user_id = ? ORDER BY name', (user_id,)
    ).fetchall()
    investment_accounts = get_investment_accounts(db, user_id)
    sources = {}
    for source, config in BANK_IMPORT_SOURCES.items():
        settings = get_user_setting(db, user_id, f'bank_import.{source}', {}) or {}
        if 'transfer_patterns' not in settings:
            settings['transfer_patterns'] = list(config.get('default_transfer_patterns', []))
        entry = {
            'label': config['label'],
            'folder': config['folder'].replace(os.sep, '/'),
            'extensions': config['extensions'],
            'multi_currency': config['multi_currency'],
            'investments': config['investments'],
            'files': list_bank_import_files(source),
            'currencies': [],
            'error': None,
            'settings': settings,
        }
        try:
            data = load_bank_source_rows(source)
            counts = defaultdict(int)
            for row in data['rows']:
                counts[row['currency']] += 1
            entry['currencies'] = [
                {'currency': currency, 'count': counts[currency]}
                for currency in sorted(counts, key=lambda cur: -counts[cur])
            ]
        except ValueError as exc:
            entry['error'] = str(exc)
        sources[source] = entry
    return accounts, investment_accounts, sources


@app.route('/import/bank')
@login_required
def bank_import():
    db = get_db()
    accounts, investment_accounts, sources = build_bank_import_context(db, current_user.id)
    return render_template(
        'import_bank.html',
        accounts=accounts,
        investment_accounts=investment_accounts,
        sources=sources,
        preview=None,
        preview_source=None,
        preview_settings_json=None,
    )


@app.route('/import/bank/preview', methods=['POST'])
@login_required
def bank_import_preview():
    db = get_db()
    source = request.form.get('source', '')
    if source not in BANK_IMPORT_SOURCES:
        flash('Unknown bank import source.', 'danger')
        return redirect(url_for('bank_import'))

    config = BANK_IMPORT_SOURCES[source]
    upload = request.files.get('new_file')
    if upload and upload.filename:
        filename = secure_filename(upload.filename)
        if not filename.lower().endswith(config['extensions']):
            flash(f"Only {' / '.join(config['extensions'])} files can be added here.", 'danger')
            return redirect(url_for('bank_import'))
        os.makedirs(config['folder'], exist_ok=True)
        upload.save(os.path.join(config['folder'], filename))
        flash(f"Added {filename} to the {config['label']} folder.", 'info')

    settings = parse_bank_import_settings_from_form(source)
    error = validate_bank_import_settings(db, current_user.id, source, settings)
    if error:
        flash(error, 'danger')
        return redirect(url_for('bank_import'))

    try:
        plan = build_bank_import_plan(db, current_user.id, source, settings)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('bank_import'))

    accounts, investment_accounts, sources = build_bank_import_context(db, current_user.id)
    # Show the submitted (not yet saved) settings for the previewed source.
    sources[source]['settings'] = settings
    return render_template(
        'import_bank.html',
        accounts=accounts,
        investment_accounts=investment_accounts,
        sources=sources,
        preview=plan,
        preview_source=source,
        preview_settings_json=json.dumps(settings),
    )


@app.route('/import/bank/confirm', methods=['POST'])
@login_required
def bank_import_confirm():
    db = get_db()
    source = request.form.get('source', '')
    if source not in BANK_IMPORT_SOURCES:
        flash('Unknown bank import source.', 'danger')
        return redirect(url_for('bank_import'))

    try:
        settings = json.loads(request.form.get('settings_json') or '{}')
        if not isinstance(settings, dict):
            raise ValueError
    except ValueError:
        flash('Invalid import settings.', 'danger')
        return redirect(url_for('bank_import'))

    error = validate_bank_import_settings(db, current_user.id, source, settings)
    if error:
        flash(error, 'danger')
        return redirect(url_for('bank_import'))

    try:
        # Re-build from the files on disk: cookie sessions cannot carry the rows
        # and re-parsing ~2k rows is effectively free.
        plan = build_bank_import_plan(db, current_user.id, source, settings)
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('bank_import'))

    imported, updated, migrated, errors = upsert_bank_transactions(db, current_user.id, plan['rows'])

    ops_created = ops_skipped = holdings_created = 0
    if plan['investment_plan']:
        ops_created, ops_skipped, holdings_created = import_tr_investment_ops(
            db, current_user.id, plan['investment_plan']['account_id'], plan['investment_plan']['rows']
        )

    set_user_setting(db, current_user.id, f'bank_import.{source}', settings)
    db.commit()

    label = BANK_IMPORT_SOURCES[source]['label']
    flash(
        f'{label}: imported {imported} new, updated {updated} existing'
        + (f', migrated {migrated} legacy' if migrated else '')
        + ' transactions.',
        'success',
    )
    if plan['investment_plan']:
        flash(
            f'Investments: created {ops_created} operations'
            + (f' and {holdings_created} holdings' if holdings_created else '')
            + (f', skipped {ops_skipped} already imported' if ops_skipped else '')
            + '.',
            'success' if ops_created or not ops_skipped else 'info',
        )
    if errors and len(errors) <= 5:
        for error_message in errors:
            flash(error_message, 'danger')
    elif errors:
        flash(f'There were {len(errors)} errors during import.', 'danger')

    return redirect(url_for('transactions'))


def build_mintos_import_context(db, user_id):
    investment_accounts = get_investment_accounts(db, user_id)
    mintos_holdings = db.execute(
        '''SELECT h.id, h.account_id, h.asset_name, a.name AS account_name
           FROM investment_holdings h
           JOIN accounts a ON a.id = h.account_id
           WHERE h.user_id = ? AND h.asset_class = 'Mintos Lending'
           ORDER BY a.name, h.asset_name''',
        (user_id,),
    ).fetchall()
    settings = get_user_setting(db, user_id, 'mintos_import', {}) or {}
    return investment_accounts, mintos_holdings, list_mintos_files(), settings


def parse_mintos_import_settings(db, user_id):
    """Read + validate the Mintos import form. Returns (settings, error)."""
    account_id = request.form.get('account_id', type=int)
    holding_id = request.form.get('holding_id', type=int)
    create_holding = request.form.get('create_holding') == 'on'
    if not account_id:
        return None, 'Select an investment account.'
    valid_account_ids = {acc['id'] for acc in get_investment_accounts(db, user_id)}
    if account_id not in valid_account_ids:
        return None, 'Invalid investment account selected.'
    if holding_id:
        owned = db.execute(
            '''SELECT id FROM investment_holdings
               WHERE id = ? AND user_id = ? AND account_id = ? AND asset_class = 'Mintos Lending' ''',
            (holding_id, user_id, account_id),
        ).fetchone()
        if not owned:
            return None, 'Invalid Mintos holding selected.'
    return {'account_id': account_id, 'holding_id': holding_id, 'create_holding': create_holding}, None


@app.route('/import/mintos')
@login_required
def mintos_import():
    db = get_db()
    investment_accounts, mintos_holdings, files, settings = build_mintos_import_context(db, current_user.id)
    return render_template(
        'import_mintos.html',
        investment_accounts=investment_accounts,
        mintos_holdings=mintos_holdings,
        files=files,
        settings=settings,
        preview=None,
        preview_settings_json=None,
    )


@app.route('/import/mintos/preview', methods=['POST'])
@login_required
def mintos_import_preview():
    db = get_db()
    upload = request.files.get('new_file')
    if upload and upload.filename:
        filename = secure_filename(upload.filename)
        if not filename.lower().endswith(MINTOS_EXTENSIONS):
            flash(f"Only {' / '.join(MINTOS_EXTENSIONS)} files can be added here.", 'danger')
            return redirect(url_for('mintos_import'))
        os.makedirs(MINTOS_FOLDER, exist_ok=True)
        upload.save(os.path.join(MINTOS_FOLDER, filename))
        flash(f'Added {filename} to the Mintos folder.', 'info')

    settings, error = parse_mintos_import_settings(db, current_user.id)
    if error:
        flash(error, 'danger')
        return redirect(url_for('mintos_import'))

    try:
        plan = build_mintos_import_plan(
            db, current_user.id, settings['account_id'], settings['holding_id'], settings['create_holding']
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('mintos_import'))

    investment_accounts, mintos_holdings, files, _ = build_mintos_import_context(db, current_user.id)
    return render_template(
        'import_mintos.html',
        investment_accounts=investment_accounts,
        mintos_holdings=mintos_holdings,
        files=files,
        settings=settings,
        preview=plan,
        preview_settings_json=json.dumps(settings),
    )


@app.route('/import/mintos/confirm', methods=['POST'])
@login_required
def mintos_import_confirm():
    db = get_db()
    try:
        settings = json.loads(request.form.get('settings_json') or '{}')
        if not isinstance(settings, dict):
            raise ValueError
    except ValueError:
        flash('Invalid import settings.', 'danger')
        return redirect(url_for('mintos_import'))

    account_id = settings.get('account_id')
    valid_account_ids = {acc['id'] for acc in get_investment_accounts(db, current_user.id)}
    if account_id not in valid_account_ids:
        flash('Invalid investment account selected.', 'danger')
        return redirect(url_for('mintos_import'))

    holding = None
    holding_id = settings.get('holding_id')
    if holding_id:
        holding = db.execute(
            '''SELECT * FROM investment_holdings
               WHERE id = ? AND user_id = ? AND account_id = ? AND asset_class = 'Mintos Lending' ''',
            (holding_id, current_user.id, account_id),
        ).fetchone()
    if not holding:
        holding = find_or_create_mintos_holding(
            db, current_user.id, account_id, create_if_missing=bool(settings.get('create_holding'))
        )
    if not holding:
        flash('No Mintos Lending holding in that account. Enable "create holding" or pick one.', 'danger')
        return redirect(url_for('mintos_import'))

    try:
        plan = build_mintos_import_plan(
            db, current_user.id, account_id, holding['id'], bool(settings.get('create_holding'))
        )
    except ValueError as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('mintos_import'))

    result = import_mintos_statement(db, current_user.id, holding, plan)
    set_user_setting(db, current_user.id, 'mintos_import', {
        'account_id': account_id,
        'holding_id': holding['id'],
        'create_holding': bool(settings.get('create_holding')),
    })
    db.commit()

    flash(
        f"Mintos: added {result['deposits_created']} deposit(s); "
        f"value {result['valuation_action']} at {format_currency(plan['current_total_value'], plan['currency'])}.",
        'success',
    )
    return redirect(url_for('investments'))


CSV_IMPORT_COLUMNS = ('ID_TRANSACTION', 'DATE', 'AMOUNT', 'CATEGORY', 'DESCRIPTION', 'PAYEE', 'TRANSACTION_TYPE')
# Column order of a header-less file (the original 5-column format).
CSV_IMPORT_POSITIONAL = {'ID_TRANSACTION': 0, 'DATE': 1, 'AMOUNT': 2, 'CATEGORY': 3, 'DESCRIPTION': 4}


def decode_uploaded_text(raw):
    """Bank exports are UTF-8 (often with a BOM) or Windows-1252."""
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('cp1252')


@app.route('/import', methods=['GET', 'POST'])
@login_required
def import_csv():
    db = get_db()
    accounts = db.execute('SELECT * FROM accounts WHERE user_id = ? ORDER BY name',
                         (current_user.id,)).fetchall()
    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()
    categories_by_id = {cat['id']: cat for cat in categories}

    if request.method == 'POST':
        file = request.files.get('file')
        if not file or not file.filename:
            flash('No file selected.', 'danger')
            return redirect(url_for('import_csv'))

        if not file.filename.lower().endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(url_for('import_csv'))

        account = next((acc for acc in accounts if str(acc['id']) == request.form.get('account_id')), None)
        if not account:
            flash('Please select an account.', 'danger')
            return redirect(url_for('import_csv'))

        default_category_id = request.form.get('default_category_id', type=int)
        if default_category_id not in categories_by_id:
            default_category_id = None
        skip_first_row = request.form.get('skip_header') == 'on'

        try:
            content = decode_uploaded_text(file.read())
            rows = list(csv.reader(io.StringIO(content), delimiter=detect_delimiter(content[:2000])))
        except Exception as e:
            flash(f'Error reading CSV file: {e}', 'danger')
            return redirect(url_for('import_csv'))

        columns = CSV_IMPORT_POSITIONAL
        first_row_num = 1
        if skip_first_row and rows:
            header = [col.strip().lstrip('﻿').upper() for col in rows[0]]
            if not {'DATE', 'AMOUNT'} <= set(header):
                flash(
                    'Invalid CSV header. Expected DATE and AMOUNT columns, optionally with '
                    'ID_TRANSACTION, CATEGORY, DESCRIPTION, PAYEE and TRANSACTION_TYPE.',
                    'danger'
                )
                return redirect(url_for('import_csv'))
            columns = {name: header.index(name) for name in CSV_IMPORT_COLUMNS if name in header}
            rows = rows[1:]
            first_row_num = 2

        set_user_setting(db, current_user.id, 'csv_import_account_id', account['id'])
        rules = load_category_rules(db, current_user.id)
        counts = {'inserted': 0, 'updated': 0}
        skipped_count = 0
        errors = []
        occurrences = defaultdict(int)

        for row_num, row in enumerate(rows, first_row_num):
            def cell(name):
                index = columns.get(name)
                return row[index].strip() if index is not None and index < len(row) else ''

            if not any(value.strip() for value in row):
                continue
            try:
                date_value = parse_import_date(cell('DATE'))
                amount = parse_amount_string(cell('AMOUNT'))
            except ValueError as exc:
                errors.append(f'Row {row_num}: {exc}')
                skipped_count += 1
                continue

            description = cell('DESCRIPTION')
            payee = cell('PAYEE')
            category_csv = cell('CATEGORY')
            trans_type = cell('TRANSACTION_TYPE').lower()
            if trans_type not in ('income', 'expense', 'transfer'):
                trans_type = 'income' if amount >= 0 else 'expense'

            matched_category_id = match_import_category(
                current_user.id, rules, categories, description, payee, category_csv
            ) or default_category_id
            matched_category = categories_by_id.get(matched_category_id)
            # Transfer categories should be imported as transfer regardless of amount sign.
            if (matched_category and matched_category['category_type'] == 'transfer') or is_transfer_like_category(category_csv):
                trans_type = 'transfer'

            id_transaction = cell('ID_TRANSACTION')
            if not id_transaction:
                # Stable id from the row content so re-importing the file updates
                # instead of duplicating (occurrence index keeps identical rows apart).
                key = (account['id'], date_value, f'{amount:.2f}', description, payee)
                occurrences[key] += 1
                id_transaction = synthetic_bank_id('csv', *key, occurrences[key])

            try:
                outcome = upsert_transaction_by_external_id(
                    db, current_user.id,
                    {
                        'account_id': account['id'],
                        'category_id': matched_category_id,
                        'transaction_type': trans_type,
                        'amount': abs(amount),
                        'description': description,
                        'payee': payee,
                        'date': date_value,
                        'id_transaction': id_transaction,
                    },
                    [id_transaction],
                    account_scope=account['id'],
                )
                counts[outcome] += 1
            except Exception as e:
                errors.append(f"Row {row_num}: {e}")
                skipped_count += 1

        db.commit()

        if counts['inserted'] or counts['updated']:
            flash(
                f"Successfully imported {counts['inserted']} new transactions and updated {counts['updated']} existing transactions.",
                'success'
            )
        if skipped_count > 0:
            flash(f'Skipped {skipped_count} rows.', 'warning')
        if errors and len(errors) <= 5:
            for error in errors:
                flash(error, 'danger')
        elif errors:
            flash(f'There were {len(errors)} errors during import.', 'danger')

        return redirect(url_for('transactions'))

    return render_template(
        'import.html', accounts=accounts, categories=categories,
        last_account_id=get_user_setting(db, current_user.id, 'csv_import_account_id'),
    )


@app.route('/export/transactions')
@login_required
def export_transactions():
    db = get_db()
    filters = {
        'account_id': request.args.get('account_id', type=int),
        'category_id': request.args.get('category_id', type=int),
        'type': request.args.get('type'),
        'start_date': request.args.get('start_date'),
        'end_date': request.args.get('end_date'),
        'search': request.args.get('search', ''),
    }

    rows = fetch_transactions_export_rows(db, current_user.id, filters)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    payload = rows_to_csv_bytes(TRANSACTION_EXPORT_HEADERS, rows)
    return send_file(
        io.BytesIO(payload),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'transactions_{timestamp}.csv',
    )


@app.route('/export/categories')
@login_required
def export_categories():
    db = get_db()
    rows = fetch_categories_export_rows(db, current_user.id)
    headers = ['NAME', 'CATEGORY_TYPE', 'ICON', 'COLOR']
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    payload = rows_to_csv_bytes(headers, rows)
    return send_file(
        io.BytesIO(payload),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'categories_{timestamp}.csv',
    )


@app.route('/export/rules')
@login_required
def export_rules():
    db = get_db()
    rows = fetch_rules_export_rows(db, current_user.id)
    headers = ['PATTERN', 'CATEGORY_NAME', 'CATEGORY_TYPE', 'MATCH_FIELD', 'IS_REGEX', 'PRIORITY']
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    payload = rows_to_csv_bytes(headers, rows)
    return send_file(
        io.BytesIO(payload),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'rules_{timestamp}.csv',
    )


@app.route('/export/full.zip')
@login_required
def export_full_zip():
    db = get_db()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    account_rows = fetch_accounts_export_rows(db, current_user.id)
    category_rows = fetch_categories_export_rows(db, current_user.id)
    rule_rows = fetch_rules_export_rows(db, current_user.id)
    tx_rows = fetch_transactions_export_rows(db, current_user.id)

    headers_accounts = ['ACCOUNT_NAME', 'ACCOUNT_TYPE', 'CURRENCY', 'CURRENT_BALANCE', 'CREATED_AT']
    headers_categories = ['NAME', 'CATEGORY_TYPE', 'ICON', 'COLOR']
    headers_rules = ['PATTERN', 'CATEGORY_NAME', 'CATEGORY_TYPE', 'MATCH_FIELD', 'IS_REGEX', 'PRIORITY']

    package_bytes = io.BytesIO()
    with zipfile.ZipFile(package_bytes, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('accounts.csv', rows_to_csv_bytes(headers_accounts, account_rows))
        archive.writestr('categories.csv', rows_to_csv_bytes(headers_categories, category_rows))
        archive.writestr('rules.csv', rows_to_csv_bytes(headers_rules, rule_rows))

        grouped_transactions = defaultdict(list)
        for row in tx_rows:
            grouped_transactions[row['ACCOUNT_NAME']].append(row)

        for account_name, rows in grouped_transactions.items():
            slug = slugify_filename(account_name)
            archive.writestr(
                f'transactions_{slug}.csv',
                rows_to_csv_bytes(TRANSACTION_EXPORT_HEADERS, rows),
            )

        manifest = {
            'format_version': '1.0',
            'generated_at': datetime.now().isoformat(),
            'user_id': current_user.id,
            'counts': {
                'accounts': len(account_rows),
                'categories': len(category_rows),
                'rules': len(rule_rows),
                'transactions': len(tx_rows),
            },
        }
        archive.writestr('manifest.json', json.dumps(manifest, indent=2))

    package_bytes.seek(0)
    return send_file(
        package_bytes,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'financetracker_backup_{timestamp}.zip',
    )


@app.route('/export/database')
@login_required
def export_database():
    """Lossless backup: a consistent snapshot of the whole SQLite database
    (investments, budgets, settings and all). Restore by replacing finance.db."""
    db = get_db()
    if db.execute('SELECT COUNT(*) FROM users').fetchone()[0] > 1:
        flash('The database backup contains every user, so it is only available while you are the only user.', 'warning')
        return redirect(url_for('options'))

    snapshot = sqlite3.connect(':memory:')
    try:
        db.backup(snapshot)
        payload = snapshot.serialize()
    finally:
        snapshot.close()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return send_file(
        io.BytesIO(payload),
        mimetype='application/vnd.sqlite3',
        as_attachment=True,
        download_name=f'finance_{timestamp}.db',
    )


@app.route('/export/full.xlsx')
@login_required
def export_full_xlsx():
    try:
        openpyxl = importlib.import_module('openpyxl')
    except ImportError:
        flash('openpyxl is required for XLSX export. Install it from requirements.txt.', 'danger')
        return redirect(url_for('import_csv'))

    db = get_db()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    account_rows = fetch_accounts_export_rows(db, current_user.id)
    category_rows = fetch_categories_export_rows(db, current_user.id)
    rule_rows = fetch_rules_export_rows(db, current_user.id)
    tx_rows = fetch_transactions_export_rows(db, current_user.id)

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)

    def write_sheet(name, headers, rows):
        ws = workbook.create_sheet(name)
        ws.append(headers)
        for row in rows:
            ws.append([row.get(header, '') for header in headers])

    metadata_rows = [
        {'KEY': 'format_version', 'VALUE': '1.0'},
        {'KEY': 'generated_at', 'VALUE': datetime.now().isoformat()},
        {'KEY': 'user_id', 'VALUE': str(current_user.id)},
        {'KEY': 'accounts_count', 'VALUE': str(len(account_rows))},
        {'KEY': 'categories_count', 'VALUE': str(len(category_rows))},
        {'KEY': 'rules_count', 'VALUE': str(len(rule_rows))},
        {'KEY': 'transactions_count', 'VALUE': str(len(tx_rows))},
    ]

    write_sheet('Metadata', ['KEY', 'VALUE'], metadata_rows)
    write_sheet('Accounts', ['ACCOUNT_NAME', 'ACCOUNT_TYPE', 'CURRENCY', 'CURRENT_BALANCE', 'CREATED_AT'], account_rows)
    write_sheet('Categories', ['NAME', 'CATEGORY_TYPE', 'ICON', 'COLOR'], category_rows)
    write_sheet('Rules', ['PATTERN', 'CATEGORY_NAME', 'CATEGORY_TYPE', 'MATCH_FIELD', 'IS_REGEX', 'PRIORITY'], rule_rows)
    write_sheet('Transactions', TRANSACTION_EXPORT_HEADERS, tx_rows)

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f'financetracker_backup_{timestamp}.xlsx',
    )


@app.route('/import/categories', methods=['POST'])
@login_required
def import_categories_csv():
    if 'categories_file' not in request.files:
        flash('No categories file selected.', 'danger')
        return redirect(url_for('import_csv'))

    file = request.files['categories_file']
    if not file or not file.filename:
        flash('No categories file selected.', 'danger')
        return redirect(url_for('import_csv'))

    db = get_db()
    try:
        rows = csv_text_to_dict_rows(file.read().decode('utf-8'))
        _, inserted, updated = upsert_categories_from_rows(db, current_user.id, rows)
        db.commit()
        flash(f'Categories import complete: {inserted} inserted, {updated} updated.', 'success')
    except Exception as exc:
        flash(f'Failed to import categories: {exc}', 'danger')

    return redirect(url_for('import_csv'))


@app.route('/import/rules', methods=['POST'])
@login_required
def import_rules_csv():
    if 'rules_file' not in request.files:
        flash('No rules file selected.', 'danger')
        return redirect(url_for('import_csv'))

    file = request.files['rules_file']
    if not file or not file.filename:
        flash('No rules file selected.', 'danger')
        return redirect(url_for('import_csv'))

    db = get_db()
    try:
        rows = csv_text_to_dict_rows(file.read().decode('utf-8'))
        categories = db.execute(
            'SELECT id, name, category_type FROM categories WHERE user_id = ?',
            (current_user.id,),
        ).fetchall()
        category_cache = {normalized_key(row['name'], row['category_type']): row['id'] for row in categories}

        inserted, updated, skipped, errors = upsert_rules_from_rows(db, current_user.id, rows, category_cache)
        db.commit()

        flash(
            f'Rules import complete: {inserted} inserted, {updated} updated, {skipped} skipped.',
            'success',
        )
        if errors:
            flash(f'Rules import warnings: {len(errors)} issue(s).', 'warning')
    except Exception as exc:
        flash(f'Failed to import rules: {exc}', 'danger')

    return redirect(url_for('import_csv'))


@app.route('/import/full', methods=['POST'])
@login_required
def import_full_package():
    if 'package_file' not in request.files:
        flash('No backup package selected.', 'danger')
        return redirect(url_for('import_csv'))

    file = request.files['package_file']
    if not file or not file.filename:
        flash('No backup package selected.', 'danger')
        return redirect(url_for('import_csv'))

    filename = file.filename.lower()
    db = get_db()

    account_rows = []
    category_rows = []
    rule_rows = []
    transaction_rows = []

    try:
        if filename.endswith('.zip'):
            package = zipfile.ZipFile(io.BytesIO(file.read()))
            members = package.infolist()
            if len(members) > BACKUP_MAX_MEMBERS or any(m.file_size > BACKUP_MAX_MEMBER_BYTES for m in members):
                raise ValueError('Backup package is too large.')

            if 'accounts.csv' in package.namelist():
                account_rows = csv_text_to_dict_rows(package.read('accounts.csv').decode('utf-8'))
            if 'categories.csv' in package.namelist():
                category_rows = csv_text_to_dict_rows(package.read('categories.csv').decode('utf-8'))
            if 'rules.csv' in package.namelist():
                rule_rows = csv_text_to_dict_rows(package.read('rules.csv').decode('utf-8'))

            for name in package.namelist():
                lower_name = name.lower()
                if lower_name.startswith('transactions') and lower_name.endswith('.csv'):
                    transaction_rows.extend(csv_text_to_dict_rows(package.read(name).decode('utf-8')))

        elif filename.endswith('.xlsx'):
            raw = file.read()
            account_rows = read_xlsx_sheet_rows(io.BytesIO(raw), 'Accounts')
            category_rows = read_xlsx_sheet_rows(io.BytesIO(raw), 'Categories')
            rule_rows = read_xlsx_sheet_rows(io.BytesIO(raw), 'Rules')
            transaction_rows = read_xlsx_sheet_rows(io.BytesIO(raw), 'Transactions')
        else:
            flash('Unsupported package format. Use .zip or .xlsx.', 'danger')
            return redirect(url_for('import_csv'))

        account_cache, created_accounts = upsert_accounts_from_rows(db, current_user.id, account_rows)
        category_cache, inserted_categories, updated_categories = upsert_categories_from_rows(db, current_user.id, category_rows)
        inserted_rules, updated_rules, skipped_rules, rule_errors = upsert_rules_from_rows(
            db, current_user.id, rule_rows, category_cache
        )
        imported_tx, updated_tx, skipped_tx, tx_errors = upsert_transactions_from_rows(
            db, current_user.id, transaction_rows, account_cache, category_cache
        )

        db.commit()

        flash(
            (
                f'Backup import complete. Accounts created: {created_accounts}; '
                f'Categories inserted/updated: {inserted_categories}/{updated_categories}; '
                f'Rules inserted/updated/skipped: {inserted_rules}/{updated_rules}/{skipped_rules}; '
                f'Transactions inserted/updated/skipped: {imported_tx}/{updated_tx}/{skipped_tx}.'
            ),
            'success',
        )

        warning_count = len(rule_errors) + len(tx_errors)
        if warning_count:
            flash(f'Import completed with {warning_count} warning(s).', 'warning')
    except Exception as exc:
        db.rollback()
        flash(f'Failed to import backup package: {exc}', 'danger')

    return redirect(url_for('import_csv'))


# Category routes
@app.route('/categories')
@login_required
def categories():
    db = get_db()
    categories = db.execute('''
        SELECT c.*, COUNT(t.id) as transaction_count
        FROM categories c
        LEFT JOIN transactions t ON c.id = t.category_id
        WHERE c.user_id = ?
        GROUP BY c.id
        ORDER BY c.category_type, c.name
    ''', (current_user.id,)).fetchall()
    return render_template('categories.html', categories=categories)


@app.route('/categories/add', methods=['GET', 'POST'])
@login_required
def add_category():
    if request.method == 'POST':
        name = request.form['name'].strip()
        category_type = request.form['category_type']
        icon = request.form.get('icon', '📁').strip() or '📁'
        color = normalize_category_color(request.form.get('color'))

        if not name:
            flash('Category name is required.', 'danger')
            return render_template('category_form.html', category=None)

        db = get_db()
        db.execute(
            'INSERT INTO categories (user_id, name, category_type, icon, color) VALUES (?, ?, ?, ?, ?)',
            (current_user.id, name, category_type, icon, color)
        )
        db.commit()
        flash('Category created successfully!', 'success')
        return redirect(url_for('categories'))

    return render_template('category_form.html', category=None)


@app.route('/categories/<int:category_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_category(category_id):
    db = get_db()
    category = db.execute('SELECT * FROM categories WHERE id = ? AND user_id = ?',
                         (category_id, current_user.id)).fetchone()

    if not category:
        flash('Category not found.', 'danger')
        return redirect(url_for('categories'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        category_type = request.form['category_type']
        icon = request.form.get('icon', '📁').strip() or '📁'
        color = normalize_category_color(request.form.get('color'))

        if not name:
            flash('Category name is required.', 'danger')
            return render_template('category_form.html', category=category)

        db.execute(
            'UPDATE categories SET name = ?, category_type = ?, icon = ?, color = ? WHERE id = ? AND user_id = ?',
            (name, category_type, icon, color, category_id, current_user.id)
        )
        db.commit()
        flash('Category updated successfully!', 'success')
        return redirect(url_for('categories'))

    return render_template('category_form.html', category=category)


@app.route('/categories/<int:category_id>/delete', methods=['POST'])
@login_required
def delete_category(category_id):
    db = get_db()
    # Set category_id to NULL for transactions using this category
    db.execute('UPDATE transactions SET category_id = NULL WHERE category_id = ? AND user_id = ?',
               (category_id, current_user.id))
    # Rules and budgets are meaningless without their category.
    db.execute('DELETE FROM category_rules WHERE category_id = ? AND user_id = ?',
               (category_id, current_user.id))
    db.execute('DELETE FROM budgets WHERE category_id = ? AND user_id = ?',
               (category_id, current_user.id))
    db.execute('DELETE FROM categories WHERE id = ? AND user_id = ?',
               (category_id, current_user.id))
    db.commit()
    flash('Category deleted.', 'info')
    return redirect(url_for('categories'))


# Category Rules routes
@app.route('/rules')
@login_required
def category_rules():
    db = get_db()
    rules = db.execute('''
        SELECT r.*, c.name as category_name, c.icon as category_icon, c.color as category_color
        FROM category_rules r
        JOIN categories c ON r.category_id = c.id
        WHERE r.user_id = ?
        ORDER BY r.priority DESC, r.pattern
    ''', (current_user.id,)).fetchall()
    return render_template('rules.html', rules=rules)


def parse_rule_form(db):
    """Validate the add/edit rule form. Returns (fields, error_message)."""
    pattern = (request.form.get('pattern') or '').strip()
    category_id = request.form.get('category_id', type=int)
    is_regex = 1 if request.form.get('is_regex') == 'on' else 0
    match_field = 'payee' if request.form.get('match_field') == 'payee' else 'description'
    if not pattern or not category_id:
        return None, 'Pattern and category are required.'
    if not db.execute('SELECT 1 FROM categories WHERE id = ? AND user_id = ?',
                      (category_id, current_user.id)).fetchone():
        return None, 'Invalid category selected.'
    try:
        priority = int(request.form.get('priority') or 0)
    except ValueError:
        return None, 'Priority must be a whole number.'
    if is_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            return None, f'Invalid regular expression: {exc}'
    return {
        'pattern': pattern, 'category_id': category_id, 'match_field': match_field,
        'is_regex': is_regex, 'priority': priority,
    }, None


@app.route('/rules/add', methods=['GET', 'POST'])
@login_required
def add_rule():
    db = get_db()

    if request.method == 'POST':
        fields, error = parse_rule_form(db)
        if error:
            flash(error, 'danger')
        else:
            db.execute(
                '''INSERT INTO category_rules (user_id, category_id, pattern, match_field, is_regex, priority)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (current_user.id, fields['category_id'], fields['pattern'], fields['match_field'],
                 fields['is_regex'], fields['priority'])
            )
            db.commit()
            flash('Rule created successfully!', 'success')
            return redirect(url_for('category_rules'))

    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()
    return render_template('rule_form.html', rule=None, categories=categories)


@app.route('/rules/<int:rule_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_rule(rule_id):
    db = get_db()
    rule = db.execute('SELECT * FROM category_rules WHERE id = ? AND user_id = ?',
                     (rule_id, current_user.id)).fetchone()

    if not rule:
        flash('Rule not found.', 'danger')
        return redirect(url_for('category_rules'))

    if request.method == 'POST':
        fields, error = parse_rule_form(db)
        if error:
            flash(error, 'danger')
        else:
            db.execute(
                '''UPDATE category_rules SET pattern = ?, category_id = ?, match_field = ?, is_regex = ?, priority = ?
                   WHERE id = ? AND user_id = ?''',
                (fields['pattern'], fields['category_id'], fields['match_field'], fields['is_regex'],
                 fields['priority'], rule_id, current_user.id)
            )
            db.commit()
            flash('Rule updated successfully!', 'success')
            return redirect(url_for('category_rules'))

    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()
    return render_template('rule_form.html', rule=rule, categories=categories)


@app.route('/rules/<int:rule_id>/delete', methods=['POST'])
@login_required
def delete_rule(rule_id):
    db = get_db()
    db.execute('DELETE FROM category_rules WHERE id = ? AND user_id = ?',
               (rule_id, current_user.id))
    db.commit()
    flash('Rule deleted.', 'info')
    return redirect(url_for('category_rules'))


# Transaction Clearing routes
@app.route('/clear')
@login_required
def clear_transactions():
    db = get_db()

    # Get uncategorized transactions
    transactions = db.execute('''
        SELECT t.*, a.name as account_name, a.currency as account_currency
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ? AND t.category_id IS NULL
        ORDER BY t.date DESC, t.id DESC
    ''', (current_user.id,)).fetchall()

    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()

    return render_template('clear.html', transactions=transactions, categories=categories)


@app.route('/api/transaction/<int:transaction_id>/categorize', methods=['POST'])
@login_required
def api_categorize_transaction(transaction_id):
    db = get_db()
    data = request.get_json()
    category_id = data.get('category_id')

    if not category_id:
        return jsonify({'error': 'Category ID required'}), 400

    # Verify transaction belongs to user
    trans = db.execute('SELECT * FROM transactions WHERE id = ? AND user_id = ?',
                      (transaction_id, current_user.id)).fetchone()
    if not trans:
        return jsonify({'error': 'Transaction not found'}), 404

    db.execute('UPDATE transactions SET category_id = ? WHERE id = ? AND user_id = ?',
               (category_id, transaction_id, current_user.id))
    db.commit()

    return jsonify({'success': True})


@app.route('/api/transaction/<int:transaction_id>/create-rule', methods=['POST'])
@login_required
def api_create_rule_from_transaction(transaction_id):
    db = get_db()
    data = request.get_json()
    category_id = data.get('category_id')
    pattern = data.get('pattern')

    if not category_id or not pattern:
        return jsonify({'error': 'Category ID and pattern required'}), 400

    # Verify transaction belongs to user
    trans = db.execute('SELECT * FROM transactions WHERE id = ? AND user_id = ?',
                      (transaction_id, current_user.id)).fetchone()
    if not trans:
        return jsonify({'error': 'Transaction not found'}), 404

    # Create the rule
    db.execute(
        '''INSERT INTO category_rules (user_id, category_id, pattern, match_field, is_regex, priority)
           VALUES (?, ?, ?, 'description', 0, 0)''',
        (current_user.id, category_id, pattern)
    )

    # Categorize the current transaction
    db.execute('UPDATE transactions SET category_id = ? WHERE id = ? AND user_id = ?',
               (category_id, transaction_id, current_user.id))

    db.commit()

    return jsonify({'success': True})


@app.route('/api/auto-categorize', methods=['POST'])
@login_required
def api_auto_categorize_all():
    db = get_db()

    # Get all uncategorized transactions
    transactions = db.execute('''
        SELECT id, description, payee FROM transactions
        WHERE user_id = ? AND category_id IS NULL
    ''', (current_user.id,)).fetchall()

    rules = load_category_rules(db, current_user.id)
    categorized_count = 0
    for trans in transactions:
        category_id = auto_categorize_transaction(
            current_user.id, trans['description'] or '', trans['payee'] or '', rules=rules
        )
        if category_id:
            db.execute('UPDATE transactions SET category_id = ? WHERE id = ?',
                      (category_id, trans['id']))
            categorized_count += 1

    db.commit()

    return jsonify({'success': True, 'categorized': categorized_count})


# Budget routes
@app.route('/budgets')
@login_required
def budgets():
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)
    ensure_daily_rates(db, base_currency)

    # Get current month
    current_month = datetime.now().strftime('%Y-%m')
    selected_month = request.args.get('month', current_month)

    # Get available months
    available_months = db.execute('''
        SELECT DISTINCT strftime('%Y-%m', date) as month
        FROM transactions
        WHERE user_id = ?
        ORDER BY month DESC
    ''', (current_user.id,)).fetchall()

    # Get budget definitions.
    budgets = db.execute('''
        SELECT b.*, c.name as category_name, c.icon as category_icon, c.color as category_color
        FROM budgets b
        JOIN categories c ON b.category_id = c.id
        WHERE b.user_id = ?
        ORDER BY c.name
    ''', (current_user.id,)).fetchall()

    spending_rows = db.execute('''
        SELECT t.category_id, t.amount, a.currency as account_currency
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ?
          AND t.transaction_type = 'expense'
          AND strftime('%Y-%m', t.date) = ?
    ''', (current_user.id, selected_month)).fetchall()

    spent_by_category = defaultdict(float)
    for row in spending_rows:
        spent_by_category[row['category_id']] += convert_amount(
            db,
            row['amount'],
            row['account_currency'],
            base_currency,
        )

    budget_items = []
    for budget in budgets:
        item = dict(budget)
        item['spent'] = spent_by_category.get(budget['category_id'], 0.0)
        budget_items.append(item)

    # Calculate totals
    total_budget = sum(b['amount'] for b in budget_items)
    total_spent = sum(b['spent'] for b in budget_items)

    return render_template('budgets.html',
                         budgets=budget_items,
                         selected_month=selected_month,
                         available_months=available_months,
                         total_budget=total_budget,
                         total_spent=total_spent,
                         base_currency=base_currency)


@app.route('/budgets/add', methods=['GET', 'POST'])
@login_required
def add_budget():
    db = get_db()

    if request.method == 'POST':
        category_id = request.form['category_id']
        amount = form_amount('amount', 'Amount')

        if not category_id or amount <= 0:
            flash('Please select a category and enter a valid amount.', 'danger')
        else:
            # Check if budget already exists for this category
            existing = db.execute(
                'SELECT id FROM budgets WHERE user_id = ? AND category_id = ?',
                (current_user.id, category_id)
            ).fetchone()

            if existing:
                flash('A budget already exists for this category. Edit the existing one instead.', 'warning')
            else:
                db.execute(
                    'INSERT INTO budgets (user_id, category_id, amount) VALUES (?, ?, ?)',
                    (current_user.id, category_id, amount)
                )
                db.commit()
                flash('Budget created successfully!', 'success')
                return redirect(url_for('budgets'))

    # Get expense categories without existing budgets
    categories = db.execute('''
        SELECT c.* FROM categories c
        LEFT JOIN budgets b ON c.id = b.category_id AND b.user_id = ?
        WHERE c.user_id = ? AND c.category_type = 'expense' AND b.id IS NULL
        ORDER BY c.name
    ''', (current_user.id, current_user.id)).fetchall()

    return render_template('budget_form.html', budget=None, categories=categories)


@app.route('/budgets/<int:budget_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_budget(budget_id):
    db = get_db()
    budget = db.execute('''
        SELECT b.*, c.name as category_name, c.icon as category_icon
        FROM budgets b
        JOIN categories c ON b.category_id = c.id
        WHERE b.id = ? AND b.user_id = ?
    ''', (budget_id, current_user.id)).fetchone()

    if not budget:
        flash('Budget not found.', 'danger')
        return redirect(url_for('budgets'))

    if request.method == 'POST':
        amount = form_amount('amount', 'Amount')

        if amount <= 0:
            flash('Please enter a valid amount.', 'danger')
        else:
            db.execute(
                'UPDATE budgets SET amount = ? WHERE id = ? AND user_id = ?',
                (amount, budget_id, current_user.id)
            )
            db.commit()
            flash('Budget updated successfully!', 'success')
            return redirect(url_for('budgets'))

    return render_template('budget_form.html', budget=budget, categories=[])


@app.route('/budgets/<int:budget_id>/delete', methods=['POST'])
@login_required
def delete_budget(budget_id):
    db = get_db()
    db.execute('DELETE FROM budgets WHERE id = ? AND user_id = ?',
               (budget_id, current_user.id))
    db.commit()
    flash('Budget deleted.', 'info')
    return redirect(url_for('budgets'))


# Reports routes
@app.route('/reports')
@login_required
def reports():
    db = get_db()
    base_currency = get_user_base_currency(current_user.id)
    ensure_daily_rates(db, base_currency)

    # Monthly chart covers the current month and the five before it.
    end_date = datetime.now()
    first_month = end_date.year * 12 + end_date.month - 1 - 5
    start_date = datetime(first_month // 12, first_month % 12 + 1, 1)
    monthly_data = monthly_income_expense(
        db, current_user.id, base_currency, start_date=start_date.strftime('%Y-%m-%d')
    )

    # Get selected month for category breakdown (default: current month)
    current_month = end_date.strftime('%Y-%m')
    selected_month = request.args.get('month', current_month)

    # Get list of available months for the dropdown
    available_months = db.execute('''
        SELECT DISTINCT strftime('%Y-%m', date) as month
        FROM transactions
        WHERE user_id = ?
        ORDER BY month DESC
    ''', (current_user.id,)).fetchall()

    category_data = expense_totals_by_category(db, current_user.id, base_currency, selected_month)

    payee_rows = db.execute('''
        SELECT COALESCE(t.payee, '') as payee, t.amount, a.currency as account_currency
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ?
          AND t.transaction_type = 'expense'
          AND t.payee != ''
          AND t.payee IS NOT NULL
    ''', (current_user.id,)).fetchall()

    payee_map = {}
    for row in payee_rows:
        payee = row['payee']
        if payee not in payee_map:
            payee_map[payee] = {'payee': payee, 'total': 0.0, 'count': 0}
        payee_map[payee]['total'] += convert_amount(
            db,
            row['amount'],
            row['account_currency'],
            base_currency,
        )
        payee_map[payee]['count'] += 1

    top_payees = sorted(payee_map.values(), key=lambda item: item['total'], reverse=True)[:10]

    return render_template('reports.html',
                         monthly_data=monthly_data,
                         category_data=category_data,
                         top_payees=top_payees,
                         selected_month=selected_month,
                         available_months=available_months,
                         base_currency=base_currency)


@app.errorhandler(413)
def upload_too_large(_error):
    flash('That file is too large (limit 25 MB).', 'danger')
    return redirect(request.referrer or url_for('dashboard'))


@app.before_request
def reject_cross_site_writes():
    """CSRF guard: a state-changing request must come from one of this site's own pages."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    source = request.headers.get('Origin') or request.headers.get('Referer')
    if source is None:
        return None  # non-browser clients (curl, tests) send neither header
    if urlsplit(source).netloc != request.host:
        abort(403)
    return None


# Initialize database on first request
@app.before_request
def before_request():
    if not hasattr(app, '_db_initialized'):
        init_db()
        app._db_initialized = True


if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(host='127.0.0.1', port=8000, debug=os.environ.get('FLASK_DEBUG') == '1')
