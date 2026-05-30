import os
import csv
import io
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from functools import wraps
from urllib import request as urllib_request
import xml.etree.ElementTree as ET

from flask import Flask, render_template, request, redirect, url_for, flash, g, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# Use pbkdf2 method since scrypt may not be available on all systems
def hash_password(password):
    return generate_password_hash(password, method='pbkdf2:sha256')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['DATABASE'] = os.environ.get('DATABASE', 'finance.db')
app.config['SAAS_MODE'] = os.environ.get('SAAS_MODE', 'false').lower() == 'true'
app.config['REGISTRATION_ENABLED'] = os.environ.get('REGISTRATION_ENABLED', 'true').lower() == 'true'

DEFAULT_BASE_CURRENCY = 'EUR'
ECB_RATES_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
SUPPORTED_CURRENCIES = {'EUR', 'USD', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'SEK', 'NOK', 'DKK', 'MXN'}
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

# Register SaaS blueprint if in SaaS mode
if app.config['SAAS_MODE']:
    from saas import saas_bp, init_saas_db
    app.register_blueprint(saas_bp)


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

        CREATE TABLE IF NOT EXISTS email_inboxes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            email_address TEXT UNIQUE NOT NULL,
            email_token TEXT UNIQUE NOT NULL,
            default_account_id INTEGER,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (default_account_id) REFERENCES accounts (id)
        );

        CREATE TABLE IF NOT EXISTS email_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            inbox_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            sender_email TEXT,
            subject TEXT,
            received_at TIMESTAMP,
            parsed_vendor TEXT,
            parsed_amount REAL,
            parsed_currency TEXT DEFAULT 'EUR',
            parsed_date DATE,
            parsed_description TEXT,
            suggested_category_id INTEGER,
            raw_body TEXT,
            attachments TEXT,
            transaction_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (inbox_id) REFERENCES email_inboxes (id),
            FOREIGN KEY (suggested_category_id) REFERENCES categories (id),
            FOREIGN KEY (transaction_id) REFERENCES transactions (id)
        );

        CREATE TABLE IF NOT EXISTS exchange_rates (
            from_currency TEXT NOT NULL,
            to_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            provider TEXT DEFAULT 'ecb',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (from_currency, to_currency)
        );
    ''')

    # Add optional external transaction reference used by CSV imports.
    transaction_columns = {
        row['name'] for row in db.execute('PRAGMA table_info(transactions)').fetchall()
    }
    if 'id_transaction' not in transaction_columns:
        db.execute('ALTER TABLE transactions ADD COLUMN id_transaction TEXT')

    user_columns = {
        row['name'] for row in db.execute('PRAGMA table_info(users)').fetchall()
    }
    if 'base_currency' not in user_columns:
        db.execute(f"ALTER TABLE users ADD COLUMN base_currency TEXT DEFAULT '{DEFAULT_BASE_CURRENCY}'")

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


def ensure_daily_rates(db, base_currency):
    base_currency = normalize_currency(base_currency)
    today = datetime.now().strftime('%Y-%m-%d')

    has_today_rates = db.execute(
        '''SELECT 1 FROM exchange_rates
           WHERE to_currency = ? AND date(updated_at) = ?
           LIMIT 1''',
        (base_currency, today),
    ).fetchone()
    if has_today_rates:
        return

    try:
        sync_rates_for_base(db, base_currency)
    except Exception:
        return


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

    return upserted


def get_conversion_rate(db, from_currency, to_currency):
    from_currency = normalize_currency(from_currency)
    to_currency = normalize_currency(to_currency)

    if from_currency == to_currency:
        return 1.0

    direct = db.execute(
        '''SELECT rate FROM exchange_rates
           WHERE from_currency = ? AND to_currency = ?
           ORDER BY updated_at DESC
           LIMIT 1''',
        (from_currency, to_currency),
    ).fetchone()
    if direct and direct['rate']:
        return float(direct['rate'])

    inverse = db.execute(
        '''SELECT rate FROM exchange_rates
           WHERE from_currency = ? AND to_currency = ?
           ORDER BY updated_at DESC
           LIMIT 1''',
        (to_currency, from_currency),
    ).fetchone()
    if inverse and inverse['rate']:
        try:
            return 1.0 / float(inverse['rate'])
        except ZeroDivisionError:
            return None

    return None


def convert_amount(db, amount, from_currency, to_currency):
    rate = get_conversion_rate(db, from_currency, to_currency)
    if rate is None:
        return float(amount)
    return float(amount) * rate


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


def auto_categorize_transaction(user_id, description, payee):
    """Try to match a transaction to a category based on rules."""
    db = get_db()
    rules = db.execute('''
        SELECT r.*, c.name as category_name
        FROM category_rules r
        JOIN categories c ON r.category_id = c.id
        WHERE r.user_id = ?
        ORDER BY r.priority DESC, r.id ASC
    ''', (user_id,)).fetchall()

    text_to_match = f"{description} {payee}".lower()

    for rule in rules:
        pattern = rule['pattern'].lower()
        if rule['is_regex']:
            try:
                if re.search(pattern, text_to_match, re.IGNORECASE):
                    return rule['category_id']
            except re.error:
                continue
        else:
            if pattern in text_to_match:
                return rule['category_id']

    return None


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


def get_account_opening_date(account):
    """Use account creation date for balance adjustment entries."""
    created_at = (account['created_at'] or '').strip()
    if not created_at:
        return datetime.now().date()

    # SQLite timestamps are usually YYYY-MM-DD HH:MM:SS.
    date_part = created_at.split(' ')[0]
    try:
        return datetime.strptime(date_part, '%Y-%m-%d').date()
    except ValueError:
        return datetime.now().date()


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
@app.route('/register', methods=['GET', 'POST'])
def register():
    if not app.config['REGISTRATION_ENABLED']:
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
            next_page = request.args.get('next')
            return redirect(next_page or url_for('dashboard'))

        flash('Invalid username or password.', 'danger')

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))


# Main routes
@app.route('/')
def index():
    # In SaaS mode, show landing page for main domain
    if app.config['SAAS_MODE']:
        return redirect(url_for('saas.landing'))
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

    # Get accounts with balances
    accounts = db.execute('''
        SELECT a.*,
            COALESCE(SUM(CASE WHEN t.transaction_type = 'income' THEN t.amount
                              WHEN t.transaction_type = 'expense' THEN -t.amount
                              ELSE 0 END), 0) as calculated_balance
        FROM accounts a
        LEFT JOIN transactions t ON a.id = t.account_id
        WHERE a.user_id = ?
        GROUP BY a.id
    ''', (current_user.id,)).fetchall()

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

    # Get monthly income and expenses converted to base currency.
    current_month = datetime.now().strftime('%Y-%m')
    monthly_rows = db.execute('''
        SELECT t.amount, t.transaction_type, a.currency as account_currency
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ? AND strftime('%Y-%m', t.date) = ?
    ''', (current_user.id, current_month)).fetchall()

    monthly_income = 0.0
    monthly_expenses = 0.0
    for row in monthly_rows:
        converted = convert_amount(db, row['amount'], row['account_currency'], base_currency)
        if row['transaction_type'] == 'income':
            monthly_income += converted
        elif row['transaction_type'] == 'expense':
            monthly_expenses += converted

    # Get spending by category for current month
    category_rows = db.execute('''
        SELECT c.id, c.name, c.icon, c.color, t.amount, a.currency as account_currency
        FROM categories c
        JOIN transactions t ON c.id = t.category_id
        JOIN accounts a ON t.account_id = a.id
        WHERE c.user_id = ?
          AND c.category_type = 'expense'
          AND t.user_id = ?
          AND t.transaction_type = 'expense'
          AND strftime('%Y-%m', t.date) = ?
    ''', (current_user.id, current_user.id, current_month)).fetchall()

    category_totals = {}
    for row in category_rows:
        cat_id = row['id']
        if cat_id not in category_totals:
            category_totals[cat_id] = {
                'name': row['name'],
                'icon': row['icon'],
                'color': row['color'],
                'total': 0.0,
            }
        category_totals[cat_id]['total'] += convert_amount(
            db,
            row['amount'],
            row['account_currency'],
            base_currency,
        )

    category_spending = sorted(
        [value for value in category_totals.values() if value['total'] > 0],
        key=lambda item: item['total'],
        reverse=True,
    )[:5]

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
    accounts = db.execute('''
        SELECT a.*,
            COALESCE(SUM(CASE WHEN t.transaction_type = 'income' THEN t.amount
                              WHEN t.transaction_type = 'expense' THEN -t.amount
                              ELSE 0 END), 0) as calculated_balance
        FROM accounts a
        LEFT JOIN transactions t ON a.id = t.account_id
        WHERE a.user_id = ?
        GROUP BY a.id
        ORDER BY a.name
    ''', (current_user.id,)).fetchall()
    return render_template('accounts.html', accounts=accounts)


@app.route('/accounts/add', methods=['GET', 'POST'])
@login_required
def add_account():
    if request.method == 'POST':
        name = request.form['name'].strip()
        account_type = request.form['account_type']
        initial_balance = float(request.form.get('initial_balance', 0) or 0)
        currency = request.form.get('currency', 'USD')

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
                (current_user.id, account_id, trans_type, abs(initial_balance), 'Opening Balance', datetime.now().date())
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
        currency = request.form.get('currency', 'USD')
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


@app.route('/accounts/<int:account_id>/delete', methods=['POST'])
@login_required
def delete_account(account_id):
    db = get_db()
    # Delete transactions first
    db.execute('DELETE FROM transactions WHERE account_id = ? AND user_id = ?',
               (account_id, current_user.id))
    db.execute('DELETE FROM accounts WHERE id = ? AND user_id = ?',
               (account_id, current_user.id))
    db.commit()
    flash('Account deleted.', 'info')
    return redirect(url_for('accounts'))


# Transaction routes
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

    query += ' ORDER BY t.date DESC, t.id DESC'

    transactions = db.execute(query, params).fetchall()
    accounts = db.execute('SELECT * FROM accounts WHERE user_id = ? ORDER BY name',
                         (current_user.id,)).fetchall()
    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY name',
                           (current_user.id,)).fetchall()

    return render_template('transactions.html',
                         transactions=transactions,
                         accounts=accounts,
                         categories=categories,
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
        amount = float(request.form['amount'])
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
        amount = float(request.form['amount'])
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
@app.route('/import', methods=['GET', 'POST'])
@login_required
def import_csv():
    db = get_db()
    accounts = db.execute('SELECT * FROM accounts WHERE user_id = ? ORDER BY name',
                         (current_user.id,)).fetchall()
    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()
    categories_by_id = {str(cat['id']): cat for cat in categories}

    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected.', 'danger')
            return redirect(url_for('import_csv'))

        file = request.files['file']
        if file.filename == '':
            flash('No file selected.', 'danger')
            return redirect(url_for('import_csv'))

        if not file.filename.endswith('.csv'):
            flash('Please upload a CSV file.', 'danger')
            return redirect(url_for('import_csv'))

        account_id = request.form.get('account_id')
        if not account_id:
            flash('Please select an account.', 'danger')
            return redirect(url_for('import_csv'))

        default_category_id = request.form.get('default_category_id') or None
        skip_first_row = request.form.get('skip_header') == 'on'

        try:
            # Read and decode CSV file
            content = file.read().decode('utf-8')
            csv_file = io.StringIO(content)

            # Try to detect delimiter
            sample = content[:2000]
            if '\t' in sample:
                delimiter = '\t'
            elif ';' in sample:
                delimiter = ';'
            else:
                delimiter = ','

            reader = csv.reader(csv_file, delimiter=delimiter)

            imported_count = 0
            updated_count = 0
            skipped_count = 0
            errors = []
            expected_headers = ['ID_TRANSACTION', 'DATE', 'AMOUNT', 'CATEGORY', 'DESCRIPTION']

            for row_num, row in enumerate(reader, 1):
                # Skip header row if requested
                if skip_first_row and row_num == 1:
                    normalized_header = [col.strip().lstrip('\ufeff').upper() for col in row[:5]]
                    if len(row) < 5 or normalized_header != expected_headers:
                        flash(
                            'Invalid CSV header. Expected: ID_TRANSACTION,DATE,AMOUNT,CATEGORY,DESCRIPTION',
                            'danger'
                        )
                        return redirect(url_for('import_csv'))
                    continue

                if len(row) < 5:
                    skipped_count += 1
                    continue

                try:
                    # Parse CSV columns: ID_TRANSACTION, DATE, AMOUNT, CATEGORY, DESCRIPTION
                    id_transaction = row[0].strip() or None
                    date_str = row[1].strip()
                    amount_str = row[2].strip()
                    category_csv = row[3].strip()
                    description = row[4].strip()

                    # Parse date (YYYY-MM-DD format)
                    try:
                        parsed_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                    except ValueError:
                        errors.append(f"Row {row_num}: Invalid date format '{date_str}'. Expected YYYY-MM-DD")
                        skipped_count += 1
                        continue

                    # Parse amount (handle various number formats)
                    amount_str = amount_str.replace('"', '').replace(' ', '')

                    # Detect format based on position of comma and dot
                    if ',' in amount_str and '.' in amount_str:
                        # Find last occurrence of each
                        last_comma = amount_str.rfind(',')
                        last_dot = amount_str.rfind('.')

                        if last_comma > last_dot:
                            # European format: 1.234,56 -> comma is decimal separator
                            amount_str = amount_str.replace('.', '').replace(',', '.')
                        else:
                            # US format: 1,234.56 -> dot is decimal separator
                            amount_str = amount_str.replace(',', '')
                    elif ',' in amount_str:
                        # Only comma - could be decimal (European) or thousands (US with no decimals)
                        # If comma is followed by exactly 2 digits at end, treat as decimal
                        if re.search(r',\d{2}$', amount_str):
                            amount_str = amount_str.replace(',', '.')
                        else:
                            # Assume thousands separator
                            amount_str = amount_str.replace(',', '')

                    amount = float(amount_str)

                    # Determine transaction type based on amount
                    if amount >= 0:
                        trans_type = 'income'
                    else:
                        trans_type = 'expense'
                        amount = abs(amount)

                    # Try to auto-categorize using rules first
                    matched_category_id = auto_categorize_transaction(current_user.id, description, category_csv)

                    # If no rule matched, try to match category from CSV to existing categories
                    if not matched_category_id and category_csv:
                        for cat in categories:
                            if cat['name'].lower() in category_csv.lower() or category_csv.lower() in cat['name'].lower():
                                matched_category_id = cat['id']
                                break

                    # Fall back to default category if still not matched
                    if not matched_category_id:
                        matched_category_id = default_category_id

                    matched_category = categories_by_id.get(str(matched_category_id)) if matched_category_id else None

                    # Transfer categories should be imported as transfer regardless of amount sign.
                    if (matched_category and matched_category['category_type'] == 'transfer') or is_transfer_like_category(category_csv):
                        trans_type = 'transfer'

                    # Upsert by id_transaction when provided: newest import replaces existing data.
                    if id_transaction:
                        existing_matches = db.execute(
                            '''SELECT id FROM transactions
                               WHERE user_id = ? AND account_id = ? AND id_transaction = ?
                               ORDER BY id DESC''',
                            (current_user.id, account_id, id_transaction)
                        ).fetchall()

                        if existing_matches:
                            keep_id = existing_matches[0]['id']
                            db.execute(
                                '''UPDATE transactions
                                   SET category_id = ?, transaction_type = ?, amount = ?,
                                       description = ?, payee = ?, date = ?
                                   WHERE id = ? AND user_id = ?''',
                                (
                                    matched_category_id,
                                    trans_type,
                                    amount,
                                    description,
                                    category_csv,
                                    parsed_date,
                                    keep_id,
                                    current_user.id,
                                )
                            )

                            # Clean up any older duplicates for the same id_transaction.
                            for duplicate_row in existing_matches[1:]:
                                db.execute(
                                    'DELETE FROM transactions WHERE id = ? AND user_id = ?',
                                    (duplicate_row['id'], current_user.id)
                                )
                            updated_count += 1
                        else:
                            db.execute(
                                '''INSERT INTO transactions
                                   (user_id, account_id, category_id, transaction_type, amount,
                                    description, payee, date, id_transaction)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                (
                                    current_user.id,
                                    account_id,
                                    matched_category_id,
                                    trans_type,
                                    amount,
                                    description,
                                    category_csv,
                                    parsed_date,
                                    id_transaction,
                                )
                            )
                            imported_count += 1
                    else:
                        db.execute(
                            '''INSERT INTO transactions
                               (user_id, account_id, category_id, transaction_type, amount, description, payee, date)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                            (
                                current_user.id,
                                account_id,
                                matched_category_id,
                                trans_type,
                                amount,
                                description,
                                category_csv,
                                parsed_date,
                            )
                        )
                        imported_count += 1

                except Exception as e:
                    errors.append(f"Row {row_num}: {str(e)}")
                    skipped_count += 1
                    continue

            db.commit()

            if imported_count > 0 or updated_count > 0:
                flash(
                    f'Successfully imported {imported_count} new transactions and updated {updated_count} existing transactions.',
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

        except Exception as e:
            flash(f'Error reading CSV file: {str(e)}', 'danger')
            return redirect(url_for('import_csv'))

    return render_template('import.html', accounts=accounts, categories=categories)


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
        color = request.form.get('color', '#6c757d')

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
        color = request.form.get('color', '#6c757d')

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


@app.route('/rules/add', methods=['GET', 'POST'])
@login_required
def add_rule():
    db = get_db()

    if request.method == 'POST':
        pattern = request.form['pattern'].strip()
        category_id = request.form['category_id']
        match_field = request.form.get('match_field', 'description')
        is_regex = 1 if request.form.get('is_regex') == 'on' else 0
        priority = int(request.form.get('priority', 0))

        if not pattern or not category_id:
            flash('Pattern and category are required.', 'danger')
        else:
            db.execute(
                '''INSERT INTO category_rules (user_id, category_id, pattern, match_field, is_regex, priority)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (current_user.id, category_id, pattern, match_field, is_regex, priority)
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
        pattern = request.form['pattern'].strip()
        category_id = request.form['category_id']
        match_field = request.form.get('match_field', 'description')
        is_regex = 1 if request.form.get('is_regex') == 'on' else 0
        priority = int(request.form.get('priority', 0))

        if not pattern or not category_id:
            flash('Pattern and category are required.', 'danger')
        else:
            db.execute(
                '''UPDATE category_rules SET pattern = ?, category_id = ?, match_field = ?, is_regex = ?, priority = ?
                   WHERE id = ? AND user_id = ?''',
                (pattern, category_id, match_field, is_regex, priority, rule_id, current_user.id)
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

    categorized_count = 0
    for trans in transactions:
        category_id = auto_categorize_transaction(current_user.id, trans['description'] or '', trans['payee'] or '')
        if category_id:
            db.execute('UPDATE transactions SET category_id = ? WHERE id = ?',
                      (category_id, trans['id']))
            categorized_count += 1

    db.commit()

    return jsonify({'success': True, 'categorized': categorized_count})


# Email Inbox routes
@app.route('/email-inbox')
@login_required
def email_inbox():
    db = get_db()

    # Get or create inbox for user
    inbox = db.execute('SELECT * FROM email_inboxes WHERE user_id = ?',
                      (current_user.id,)).fetchone()

    if not inbox:
        # Create inbox for user
        from email_processor import generate_inbox_email
        email_domain = os.environ.get('EMAIL_DOMAIN', 'in.financetracker.app')
        email_address, token = generate_inbox_email(current_user.id, email_domain)

        db.execute('''
            INSERT INTO email_inboxes (user_id, email_address, email_token)
            VALUES (?, ?, ?)
        ''', (current_user.id, email_address, token))
        db.commit()

        inbox = db.execute('SELECT * FROM email_inboxes WHERE user_id = ?',
                          (current_user.id,)).fetchone()

    # Get pending email transactions
    pending_emails = db.execute('''
        SELECT e.*, c.name as category_name, c.icon as category_icon
        FROM email_transactions e
        LEFT JOIN categories c ON e.suggested_category_id = c.id
        WHERE e.user_id = ? AND e.status = 'pending'
        ORDER BY e.received_at DESC
    ''', (current_user.id,)).fetchall()

    # Get recent processed emails
    processed_emails = db.execute('''
        SELECT e.*, c.name as category_name, t.amount as transaction_amount
        FROM email_transactions e
        LEFT JOIN categories c ON e.suggested_category_id = c.id
        LEFT JOIN transactions t ON e.transaction_id = t.id
        WHERE e.user_id = ? AND e.status != 'pending'
        ORDER BY e.received_at DESC
        LIMIT 20
    ''', (current_user.id,)).fetchall()

    accounts = db.execute('SELECT * FROM accounts WHERE user_id = ? ORDER BY name',
                         (current_user.id,)).fetchall()
    categories = db.execute('SELECT * FROM categories WHERE user_id = ? ORDER BY category_type, name',
                           (current_user.id,)).fetchall()

    return render_template('email_inbox.html',
                         inbox=inbox,
                         pending_emails=pending_emails,
                         processed_emails=processed_emails,
                         accounts=accounts,
                         categories=categories)


@app.route('/email-inbox/settings', methods=['POST'])
@login_required
def email_inbox_settings():
    db = get_db()
    default_account_id = request.form.get('default_account_id') or None
    is_active = 1 if request.form.get('is_active') == 'on' else 0

    db.execute('''
        UPDATE email_inboxes SET default_account_id = ?, is_active = ?
        WHERE user_id = ?
    ''', (default_account_id, is_active, current_user.id))
    db.commit()

    flash('Email inbox settings updated.', 'success')
    return redirect(url_for('email_inbox'))


@app.route('/email-inbox/regenerate', methods=['POST'])
@login_required
def regenerate_inbox_email():
    db = get_db()
    from email_processor import generate_inbox_email

    email_domain = os.environ.get('EMAIL_DOMAIN', 'in.financetracker.app')
    email_address, token = generate_inbox_email(current_user.id, email_domain)

    db.execute('''
        UPDATE email_inboxes SET email_address = ?, email_token = ?
        WHERE user_id = ?
    ''', (email_address, token, current_user.id))
    db.commit()

    flash('New email address generated. Update your forwarding rules.', 'success')
    return redirect(url_for('email_inbox'))


# Email Webhook endpoint (receives forwarded emails)
@app.route('/api/email/inbound', methods=['POST'])
def email_inbound_webhook():
    """
    Webhook endpoint for receiving forwarded emails.
    Supports SendGrid, Mailgun, and Postmark formats.
    """
    from email_processor import (
        process_sendgrid_webhook, process_mailgun_webhook,
        process_postmark_webhook, parse_email_content
    )

    db = get_db()

    # Determine email provider and parse data
    content_type = request.content_type or ''

    if 'multipart/form-data' in content_type:
        # SendGrid format
        data = process_sendgrid_webhook(request.form.to_dict())
    elif request.is_json:
        json_data = request.get_json()
        if 'FromFull' in json_data or 'TextBody' in json_data:
            # Postmark format
            data = process_postmark_webhook(json_data)
        else:
            # Mailgun or generic JSON
            data = process_mailgun_webhook(json_data)
    else:
        # Try form data (Mailgun)
        data = process_mailgun_webhook(request.form.to_dict())

    # Extract recipient email to find inbox
    to_email = data.get('to', '').lower()

    # Handle multiple recipients
    if ',' in to_email:
        to_email = to_email.split(',')[0].strip()

    # Extract just the email address if it includes name
    email_match = re.search(r'[\w\.-]+@[\w\.-]+', to_email)
    if email_match:
        to_email = email_match.group(0)

    # Find inbox
    inbox = db.execute('SELECT * FROM email_inboxes WHERE email_address = ? AND is_active = 1',
                      (to_email,)).fetchone()

    if not inbox:
        return jsonify({'error': 'Invalid inbox'}), 404

    # Parse email content
    parsed = parse_email_content(
        data.get('subject', ''),
        data.get('body', ''),
        data.get('sender', ''),
        data.get('html_body', '')
    )

    # Try to match category
    suggested_category_id = None
    if parsed.get('category_suggestion'):
        category = db.execute(
            'SELECT id FROM categories WHERE user_id = ? AND name LIKE ?',
            (inbox['user_id'], f"%{parsed['category_suggestion']}%")
        ).fetchone()
        if category:
            suggested_category_id = category['id']

    # Also try auto-categorization rules
    if not suggested_category_id and (parsed.get('vendor') or parsed.get('description')):
        suggested_category_id = auto_categorize_transaction(
            inbox['user_id'],
            parsed.get('description', ''),
            parsed.get('vendor', '')
        )

    # Store email transaction
    db.execute('''
        INSERT INTO email_transactions (
            user_id, inbox_id, status, sender_email, subject, received_at,
            parsed_vendor, parsed_amount, parsed_currency, parsed_date,
            parsed_description, suggested_category_id, raw_body, attachments
        ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        inbox['user_id'],
        inbox['id'],
        data.get('sender', ''),
        data.get('subject', ''),
        datetime.now(),
        parsed.get('vendor'),
        parsed.get('amount'),
        parsed.get('currency', 'EUR'),
        parsed.get('date'),
        parsed.get('description'),
        suggested_category_id,
        data.get('body', '')[:10000],  # Limit body size
        json.dumps(data.get('attachments', [])) if data.get('attachments') else None
    ))
    db.commit()

    return jsonify({'success': True, 'message': 'Email processed'})


@app.route('/api/email/<int:email_id>/approve', methods=['POST'])
@login_required
def approve_email_transaction(email_id):
    """Convert pending email to actual transaction."""
    db = get_db()
    data = request.get_json()

    email_trans = db.execute(
        'SELECT * FROM email_transactions WHERE id = ? AND user_id = ?',
        (email_id, current_user.id)
    ).fetchone()

    if not email_trans:
        return jsonify({'error': 'Email not found'}), 404

    account_id = data.get('account_id')
    category_id = data.get('category_id')
    amount = data.get('amount', email_trans['parsed_amount'])
    description = data.get('description', email_trans['parsed_description'])
    date = data.get('date', email_trans['parsed_date'])

    if not account_id:
        # Use default account from inbox
        inbox = db.execute('SELECT default_account_id FROM email_inboxes WHERE id = ?',
                          (email_trans['inbox_id'],)).fetchone()
        account_id = inbox['default_account_id'] if inbox else None

    if not account_id:
        return jsonify({'error': 'Account ID required'}), 400

    if not amount:
        return jsonify({'error': 'Amount required'}), 400

    # Create transaction
    cursor = db.execute('''
        INSERT INTO transactions (user_id, account_id, category_id, transaction_type, amount, description, payee, date)
        VALUES (?, ?, ?, 'expense', ?, ?, ?, ?)
    ''', (current_user.id, account_id, category_id, amount, description, email_trans['parsed_vendor'], date))

    transaction_id = cursor.lastrowid

    # Update email transaction
    db.execute('''
        UPDATE email_transactions SET status = 'approved', transaction_id = ?
        WHERE id = ?
    ''', (transaction_id, email_id))

    db.commit()

    return jsonify({'success': True, 'transaction_id': transaction_id})


@app.route('/api/email/<int:email_id>/reject', methods=['POST'])
@login_required
def reject_email_transaction(email_id):
    """Mark email as rejected/ignored."""
    db = get_db()

    result = db.execute(
        'UPDATE email_transactions SET status = ? WHERE id = ? AND user_id = ?',
        ('rejected', email_id, current_user.id)
    )
    db.commit()

    if result.rowcount == 0:
        return jsonify({'error': 'Email not found'}), 404

    return jsonify({'success': True})


@app.route('/api/email/<int:email_id>/create-rule', methods=['POST'])
@login_required
def create_rule_from_email(email_id):
    """Create categorization rule from email and approve it."""
    db = get_db()
    data = request.get_json()

    email_trans = db.execute(
        'SELECT * FROM email_transactions WHERE id = ? AND user_id = ?',
        (email_id, current_user.id)
    ).fetchone()

    if not email_trans:
        return jsonify({'error': 'Email not found'}), 404

    pattern = data.get('pattern', email_trans['parsed_vendor'])
    category_id = data.get('category_id')

    if not pattern or not category_id:
        return jsonify({'error': 'Pattern and category required'}), 400

    # Create rule
    db.execute('''
        INSERT INTO category_rules (user_id, category_id, pattern, match_field, is_regex, priority)
        VALUES (?, ?, ?, 'description', 0, 0)
    ''', (current_user.id, category_id, pattern))

    db.commit()

    return jsonify({'success': True})


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
        amount = float(request.form['amount'])

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
        amount = float(request.form['amount'])

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

    # Get date range (default: last 6 months)
    end_date = datetime.now()
    start_date = datetime(end_date.year, end_date.month - 5 if end_date.month > 5 else end_date.month + 7, 1)
    if end_date.month <= 5:
        start_date = datetime(end_date.year - 1, start_date.month, 1)

    monthly_rows = db.execute('''
        SELECT strftime('%Y-%m', t.date) as month,
               t.transaction_type,
               t.amount,
               a.currency as account_currency
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ? AND t.date >= ?
        ORDER BY month
    ''', (current_user.id, start_date.strftime('%Y-%m-%d'))).fetchall()

    monthly_map = {}
    for row in monthly_rows:
        month = row['month']
        if month not in monthly_map:
            monthly_map[month] = {'month': month, 'income': 0.0, 'expenses': 0.0}
        converted = convert_amount(db, row['amount'], row['account_currency'], base_currency)
        if row['transaction_type'] == 'income':
            monthly_map[month]['income'] += converted
        elif row['transaction_type'] == 'expense':
            monthly_map[month]['expenses'] += converted

    monthly_data = [monthly_map[key] for key in sorted(monthly_map.keys())]

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

    category_rows = db.execute('''
        SELECT c.id, c.name, c.color, t.amount, a.currency as account_currency
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ? AND t.transaction_type = 'expense' AND strftime('%Y-%m', t.date) = ?
    ''', (current_user.id, selected_month)).fetchall()

    category_map = {}
    for row in category_rows:
        cat_id = row['id']
        if cat_id not in category_map:
            category_map[cat_id] = {'name': row['name'], 'color': row['color'], 'total': 0.0}
        category_map[cat_id]['total'] += convert_amount(
            db,
            row['amount'],
            row['account_currency'],
            base_currency,
        )

    category_data = sorted(category_map.values(), key=lambda item: item['total'], reverse=True)

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


# API endpoints for charts
@app.route('/api/monthly-data')
@login_required
def api_monthly_data():
    db = get_db()
    months = request.args.get('months', 6, type=int)
    base_currency = get_user_base_currency(current_user.id)
    ensure_daily_rates(db, base_currency)

    rows = db.execute('''
        SELECT strftime('%Y-%m', t.date) as month,
               t.transaction_type,
               t.amount,
               a.currency as account_currency
        FROM transactions t
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ?
        ORDER BY month DESC
    ''', (current_user.id,)).fetchall()

    monthly_map = {}
    for row in rows:
        month = row['month']
        if month not in monthly_map:
            monthly_map[month] = {'month': month, 'income': 0.0, 'expenses': 0.0}
        converted = convert_amount(db, row['amount'], row['account_currency'], base_currency)
        if row['transaction_type'] == 'income':
            monthly_map[month]['income'] += converted
        elif row['transaction_type'] == 'expense':
            monthly_map[month]['expenses'] += converted

    ordered = [monthly_map[key] for key in sorted(monthly_map.keys(), reverse=True)[:months]]
    ordered.reverse()
    return jsonify(ordered)


@app.route('/api/category-spending')
@login_required
def api_category_spending():
    db = get_db()
    month = request.args.get('month', datetime.now().strftime('%Y-%m'))
    base_currency = get_user_base_currency(current_user.id)
    ensure_daily_rates(db, base_currency)

    rows = db.execute('''
        SELECT c.id, c.name, c.color, t.amount, a.currency as account_currency
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        JOIN accounts a ON t.account_id = a.id
        WHERE t.user_id = ? AND t.transaction_type = 'expense' AND strftime('%Y-%m', t.date) = ?
    ''', (current_user.id, month)).fetchall()

    category_map = {}
    for row in rows:
        cat_id = row['id']
        if cat_id not in category_map:
            category_map[cat_id] = {'name': row['name'], 'color': row['color'], 'total': 0.0}
        category_map[cat_id]['total'] += convert_amount(
            db,
            row['amount'],
            row['account_currency'],
            base_currency,
        )

    data = sorted(category_map.values(), key=lambda item: item['total'], reverse=True)
    return jsonify(data)


# Initialize database on first request
@app.before_request
def before_request():
    if not hasattr(app, '_db_initialized'):
        init_db()
        app._db_initialized = True


if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(debug=True, port=8000)
