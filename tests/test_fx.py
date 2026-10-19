"""Exchange rates: persisted once a day, cross rates, and visible gaps."""
import pytest

import app as ft
from tests.conftest import fake_fetch_rates
from tests.helpers import make_account


def test_daily_rates_are_saved_and_fetched_once(client, db, monkeypatch):
    calls = []

    def counting_fetch(base_currency):
        calls.append(base_currency)
        return fake_fetch_rates(base_currency)

    monkeypatch.setattr(ft, 'fetch_ecb_rates_for_base', counting_fetch)
    client.get('/dashboard')
    client.get('/reports')

    assert calls == ['EUR']
    saved = db.execute(
        "SELECT COUNT(*) FROM exchange_rates WHERE to_currency = 'EUR' AND date(updated_at) = date('now')"
    ).fetchone()[0]
    assert saved == len(fake_fetch_rates('EUR'))


def test_cross_rate_goes_through_base_currency(app, db):
    db.executemany(
        'INSERT INTO exchange_rates (from_currency, to_currency, rate) VALUES (?, ?, ?)',
        [('USD', 'EUR', 0.9), ('GBP', 'EUR', 1.2)],
    )
    db.commit()
    assert ft.get_conversion_rate(db, 'GBP', 'USD') == pytest.approx(1.2 / 0.9)
    assert ft.get_conversion_rate(db, 'EUR', 'USD') == pytest.approx(1 / 0.9)


def test_missing_rate_is_flagged_not_silent(client, db, user_id):
    account_id = make_account(db, user_id, name='Krona', currency='SEK')
    db.execute(
        '''INSERT INTO transactions (user_id, account_id, transaction_type, amount, date)
           VALUES (?, ?, 'income', 100, '2026-01-01')''',
        (user_id, account_id),
    )
    db.commit()

    page = client.get('/dashboard').get_data(as_text=True)
    assert 'No exchange rate for' in page
    assert 'SEK' in page
