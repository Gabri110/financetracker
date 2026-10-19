"""Shared fixtures: an isolated app + DB per test, with no network and no real bank files."""
import os
import sqlite3

os.environ.setdefault('SECRET_KEY', 'test-secret')

import pytest

import app as ft

# Fixed ECB-style rates (X -> EUR) so tests never hit the network.
TEST_RATES_TO_EUR = {'EUR': 1.0, 'USD': 0.9, 'GBP': 1.2, 'CHF': 1.05}


def fake_fetch_rates(base_currency):
    base = ft.normalize_currency(base_currency)
    base_vs_eur = TEST_RATES_TO_EUR[base]
    return {code: rate / base_vs_eur for code, rate in TEST_RATES_TO_EUR.items()}


@pytest.fixture
def app(tmp_path, monkeypatch):
    raw_dir = tmp_path / 'raw'
    for key, source in ft.BANK_IMPORT_SOURCES.items():
        folder = raw_dir / key
        folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.setitem(source, 'folder', str(folder))
    mintos_dir = raw_dir / 'mintos'
    mintos_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ft, 'MINTOS_FOLDER', str(mintos_dir))
    monkeypatch.setattr(ft, 'fetch_ecb_rates_for_base', fake_fetch_rates)
    monkeypatch.setattr(ft, '_last_rate_sync_attempt', {})

    flask_app = ft.app
    flask_app.config.update(TESTING=True, DATABASE=str(tmp_path / 'test.db'))
    if hasattr(flask_app, '_db_initialized'):
        delattr(flask_app, '_db_initialized')
    with flask_app.app_context():
        ft.init_db()
    flask_app._db_initialized = True
    yield flask_app


@pytest.fixture
def client(app):
    client = app.test_client()
    client.post('/register', data={
        'username': 'tester', 'email': 'tester@example.com',
        'password': 'secret123', 'confirm_password': 'secret123',
    })
    response = client.post('/login', data={'username': 'tester', 'password': 'secret123'})
    assert response.status_code == 302
    return client


@pytest.fixture
def db(app):
    # A separate connection, not get_db(): holding an app context open for the
    # whole test would make every request reuse it (and its g / logged-in user).
    connection = sqlite3.connect(app.config['DATABASE'])
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@pytest.fixture
def user_id(client, db):
    return db.execute("SELECT id FROM users WHERE username = 'tester'").fetchone()['id']
