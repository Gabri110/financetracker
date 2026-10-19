"""Re-imports, the generic CSV import, backups, rules and deletes."""
import csv
import io
import zipfile
from decimal import Decimal

import pytest

import app as ft
from tests.helpers import add_operation, make_account, make_holding, make_investment_account


def category_id(db, user_id, name):
    return db.execute('SELECT id FROM categories WHERE user_id = ? AND name = ?', (user_id, name)).fetchone()['id']


def bank_row(account_id, amount, **overrides):
    row = {
        'account_id': account_id, 'category_text': '', 'description': 'Coffee', 'payee': 'Cafe',
        'transaction_type': 'expense' if Decimal(amount) < 0 else 'income', 'amount': Decimal(amount),
        'date': '2026-03-02', 'id_transaction': 'rev_1', 'legacy_id': None,
    }
    row.update(overrides)
    return row


def test_bank_reimport_keeps_manual_category_and_type(app, db, user_id):
    account_id = make_account(db, user_id)
    ft.upsert_bank_transactions(db, user_id, [bank_row(account_id, '-3.50')])
    groceries = category_id(db, user_id, 'Groceries')
    db.execute("UPDATE transactions SET category_id = ?, transaction_type = 'transfer'", (groceries,))
    db.commit()

    imported, updated, migrated, errors = ft.upsert_bank_transactions(db, user_id, [bank_row(account_id, '-4.00')])

    assert (imported, updated, migrated, errors) == (0, 1, 0, [])
    row = db.execute('SELECT category_id, transaction_type, amount FROM transactions').fetchone()
    assert (row['category_id'], row['transaction_type'], row['amount']) == (groceries, 'transfer', 4.0)


def test_bank_import_migrates_legacy_id(app, db, user_id):
    account_id = make_account(db, user_id)
    ft.upsert_bank_transactions(db, user_id, [bank_row(account_id, '-1', id_transaction='old-raw-id')])
    result = ft.upsert_bank_transactions(
        db, user_id, [bank_row(account_id, '-1', id_transaction='rev_new', legacy_id='old-raw-id')]
    )
    assert result[:3] == (0, 0, 1)
    assert [r[0] for r in db.execute('SELECT id_transaction FROM transactions')] == ['rev_new']


def upload_csv(client, account_id, text, encoding='utf-8', **form):
    data = {'file': (io.BytesIO(text.encode(encoding)), 'bank.CSV'), 'account_id': str(account_id),
            'skip_header': 'on', **form}
    return client.post('/import', data=data, content_type='multipart/form-data')


def test_csv_import_columns_by_name_and_idempotent(client, db, user_id):
    account_id = make_account(db, user_id)
    text = (
        'DATE;AMOUNT;DESCRIPTION;PAYEE;TRANSACTION_TYPE\n'
        '31/01/2026;-12,5;Café con leche;Bar Pepe;\n'
        '01/02/2026;100;Move to savings;;transfer\n'
    )
    upload_csv(client, account_id, text, encoding='cp1252')
    upload_csv(client, account_id, text, encoding='cp1252')  # re-import: no duplicates

    rows = db.execute(
        'SELECT date, amount, description, payee, transaction_type FROM transactions ORDER BY date'
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ('2026-01-31', 12.5, 'Café con leche', 'Bar Pepe', 'expense'),
        ('2026-02-01', 100.0, 'Move to savings', '', 'transfer'),
    ]
    # The account is remembered for next time.
    page = client.get('/import').get_data(as_text=True)
    assert f'value="{account_id}" selected' in page


def read_zip_tables(payload):
    archive = zipfile.ZipFile(io.BytesIO(payload))
    return {name: archive.read(name).decode('utf-8') for name in archive.namelist()}


def make_zip(tables):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        for name, text in tables.items():
            archive.writestr(name, text)
    return out.getvalue()


def restore(client, payload):
    return client.post('/import/full', data={'package_file': (io.BytesIO(payload), 'backup.zip')},
                       content_type='multipart/form-data')


def seed_backup_data(client, db, user_id):
    savings = make_account(db, user_id, name='Savings', account_type='savings')
    card = make_account(db, user_id, name='Card', account_type='credit')
    for account_id in (savings, savings, card):  # two identical manual entries in Savings
        client.post('/transactions/add', data={
            'account_id': account_id, 'transaction_type': 'expense', 'amount': '5',
            'description': 'Snack', 'payee': '', 'date': '2026-01-10',
        })


def test_backup_restore_into_same_db_is_a_no_op(client, db, user_id):
    seed_backup_data(client, db, user_id)
    payload = client.get('/export/full.zip').data

    restore(client, payload)

    assert db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] == 2
    assert db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 3


def test_old_backup_without_account_type_reuses_accounts(client, db, user_id):
    seed_backup_data(client, db, user_id)
    tables = read_zip_tables(client.get('/export/full.zip').data)
    for name, text in tables.items():
        if name.startswith('transactions'):
            rows = list(csv.DictReader(io.StringIO(text)))
            out = io.StringIO()
            headers = [h for h in rows[0].keys() if h != 'ACCOUNT_TYPE']
            writer = csv.DictWriter(out, fieldnames=headers, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
            tables[name] = out.getvalue()

    restore(client, make_zip(tables))

    types = sorted(r[0] for r in db.execute('SELECT account_type FROM accounts'))
    assert types == ['credit', 'savings']


def test_backup_restores_into_fresh_user(client, app, db, user_id, monkeypatch):
    seed_backup_data(client, db, user_id)
    payload = client.get('/export/full.zip').data

    monkeypatch.setitem(app.config, 'REGISTRATION_ENABLED', True)
    other = app.test_client()
    other.post('/register', data={'username': 'b', 'email': 'b@example.com',
                                  'password': 'secret123', 'confirm_password': 'secret123'})
    other.post('/login', data={'username': 'b', 'password': 'secret123'})
    restore(other, payload)

    other_id = db.execute("SELECT id FROM users WHERE username = 'b'").fetchone()['id']
    accounts = db.execute(
        '''SELECT a.name, a.account_type, COUNT(t.id) AS n FROM accounts a
           LEFT JOIN transactions t ON t.account_id = a.id
           WHERE a.user_id = ? GROUP BY a.id ORDER BY a.name''',
        (other_id,),
    ).fetchall()
    assert [tuple(r) for r in accounts] == [('Card', 'credit', 1), ('Savings', 'savings', 2)]


def add_rule(db, user_id, pattern, category, is_regex=0, match_field='description'):
    db.execute(
        'INSERT INTO category_rules (user_id, category_id, pattern, match_field, is_regex) VALUES (?, ?, ?, ?, ?)',
        (user_id, category_id(db, user_id, category), pattern, match_field, is_regex),
    )
    db.commit()


def test_regex_rules_are_not_lowercased(app, db, user_id):
    add_rule(db, user_id, r'^\D+$', 'Groceries', is_regex=1)  # lowercased this became \d
    rules = ft.load_category_rules(db, user_id)
    assert ft.auto_categorize_transaction(user_id, 'Supermarket', '', rules) == category_id(db, user_id, 'Groceries')
    assert ft.auto_categorize_transaction(user_id, 'Order 123', '', rules) is None


def test_payee_only_rules_ignore_description(app, db, user_id):
    add_rule(db, user_id, 'netflix', 'Entertainment', match_field='payee')
    rules = ft.load_category_rules(db, user_id)
    assert ft.auto_categorize_transaction(user_id, 'netflix gift card', 'Shop', rules) is None
    assert ft.auto_categorize_transaction(user_id, 'Monthly', 'NETFLIX.COM', rules) == category_id(db, user_id, 'Entertainment')


def test_invalid_regex_rule_is_rejected(client, db, user_id):
    response = client.post('/rules/add', data={
        'pattern': '([unclosed', 'category_id': category_id(db, user_id, 'Groceries'), 'is_regex': 'on',
        'priority': '',
    }, follow_redirects=True)
    assert 'Invalid regular expression' in response.get_data(as_text=True)
    assert db.execute('SELECT COUNT(*) FROM category_rules').fetchone()[0] == 0


def test_deleting_account_removes_its_investments(client, db, user_id):
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'ETF', 1, 10, 10)
    add_operation(db, user_id, account_id, holding_id, 'buy', 10, '2026-01-01')
    db.execute(
        "INSERT INTO investment_valuations (user_id, holding_id, valuation_date, total_value) VALUES (?, ?, '2026-01-02', 10)",
        (user_id, holding_id),
    )
    db.commit()

    client.post(f'/accounts/{account_id}/delete')

    for table in ('investment_holdings', 'investment_operations', 'investment_valuations', 'accounts'):
        assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table


def test_deleting_category_removes_its_rules_and_budgets(client, db, user_id):
    groceries = category_id(db, user_id, 'Groceries')
    add_rule(db, user_id, 'market', 'Groceries')
    db.execute('INSERT INTO budgets (user_id, category_id, amount) VALUES (?, ?, 100)', (user_id, groceries))
    db.commit()

    client.post(f'/categories/{groceries}/delete')

    assert db.execute('SELECT COUNT(*) FROM category_rules').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM budgets').fetchone()[0] == 0


@pytest.mark.parametrize('amount', ['abc', 'nan', 'inf', ''])
def test_bad_amount_is_a_message_not_a_crash(client, db, user_id, amount):
    account_id = make_account(db, user_id)
    response = client.post('/transactions/add', data={
        'account_id': account_id, 'transaction_type': 'expense', 'amount': amount, 'date': '2026-01-01',
    }, follow_redirects=True)
    assert response.status_code == 200
    assert 'Amount' in response.get_data(as_text=True)
    assert db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0


def test_database_backup_is_a_complete_sqlite_file(client, db, user_id, tmp_path):
    make_investment_account(db, user_id)
    response = client.get('/export/database')
    assert response.status_code == 200
    path = tmp_path / 'backup.db'
    path.write_bytes(response.data)

    import sqlite3
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT COUNT(*) FROM accounts WHERE account_type = 'investment'").fetchone()[0] == 1
    assert copy.execute('PRAGMA user_version').fetchone()[0] == len(ft.MIGRATIONS)
    copy.close()
