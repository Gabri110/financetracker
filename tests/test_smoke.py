"""Every main page renders, and the basic add-account/add-transaction flow works."""
import pytest

PAGES = [
    '/dashboard', '/accounts', '/accounts/add', '/transactions', '/transactions/add',
    '/categories', '/categories/add', '/rules', '/rules/add', '/budgets', '/budgets/add',
    '/reports', '/options', '/investments', '/import', '/import/bank', '/import/mintos', '/sync',
    '/clear',
]


@pytest.mark.parametrize('path', PAGES)
def test_page_renders(client, path):
    response = client.get(path)
    assert response.status_code == 200, path


def test_login_required(app):
    response = app.test_client().get('/dashboard')
    assert response.status_code == 302
    assert '/login' in response.headers['Location']


def test_add_account_and_transaction_updates_balance(client, db, user_id):
    client.post('/accounts/add', data={
        'name': 'Main', 'account_type': 'checking', 'initial_balance': '100', 'currency': 'EUR',
    })
    account_id = db.execute('SELECT id FROM accounts WHERE user_id = ?', (user_id,)).fetchone()['id']
    client.post('/transactions/add', data={
        'account_id': account_id, 'transaction_type': 'expense', 'amount': '30.50',
        'description': 'Groceries', 'payee': 'Shop', 'date': '2026-01-15',
    })

    page = client.get('/accounts').get_data(as_text=True)
    assert '69.50' in page


def test_transactions_are_paginated(client, db, user_id):
    account_id = db.execute(
        "INSERT INTO accounts (user_id, name, account_type, currency) VALUES (?, 'Main', 'checking', 'EUR')",
        (user_id,),
    ).lastrowid
    db.executemany(
        '''INSERT INTO transactions (user_id, account_id, transaction_type, amount, description, date)
           VALUES (?, ?, 'expense', 1, ?, '2026-01-01')''',
        [(user_id, account_id, f'row {n}') for n in range(150)],
    )
    db.commit()

    first = client.get('/transactions').get_data(as_text=True)
    second = client.get('/transactions?page=2').get_data(as_text=True)
    assert 'Showing 100 of 150' in first and 'Page 1 of 2' in first
    assert 'Showing 50 of 150' in second


def test_category_dropdowns_render_groups(client, db, user_id):
    page = client.get('/rules/add').get_data(as_text=True)
    assert '<optgroup label="Income">' in page and '<optgroup label="Transfers">' in page
    assert 'Groceries' in page
