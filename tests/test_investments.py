"""Investment returns: no double counting, correct evolution baselines, trade prices."""
from datetime import datetime
from decimal import Decimal

import pytest

import app as ft
from tests.helpers import add_operation, make_holding, make_investment_account, write_xlsx

YEAR = datetime.now().year


def dashboard_item(db, user_id, holding_id):
    data = ft.build_investment_dashboard_data(db, user_id, 'EUR')
    return next(item for item in data['holdings'] if item['id'] == holding_id)


def valuation_returns(db, holding_id):
    return [
        row['return_amount'] for row in db.execute(
            'SELECT return_amount FROM investment_valuations WHERE holding_id = ? ORDER BY valuation_date, id',
            (holding_id,),
        )
    ]


def test_mintos_evolution_return_counted_once(client, db, user_id):
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'Mintos Lending', 1, 1000, 1000)
    add_operation(db, user_id, account_id, holding_id, 'deposit', 1000, f'{YEAR}-01-10')

    client.post('/investments/evolution/add', data={
        'holding_id': holding_id, 'valuation_date': f'{YEAR}-02-01', 'total_value': '1010',
    })

    item = dashboard_item(db, user_id, holding_id)
    assert item['total_return_abs_base'] == pytest.approx(10)
    assert item['ytd_return_base'] == pytest.approx(10)
    # No synthetic 'dividend' operation is written alongside the valuation.
    assert db.execute(
        "SELECT COUNT(*) FROM investment_operations WHERE operation_type = 'dividend'"
    ).fetchone()[0] == 0
    data = ft.build_investment_dashboard_data(db, user_id, 'EUR')
    assert data['dividend_ytd'] == 0


def test_evolution_baseline_includes_deposits_since_last_valuation(client, db, user_id):
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'Mintos Lending', 1, 1000, 1000)
    add_operation(db, user_id, account_id, holding_id, 'deposit', 1000, f'{YEAR}-01-10')
    client.post('/investments/evolution/add', data={
        'holding_id': holding_id, 'valuation_date': f'{YEAR}-01-31', 'total_value': '1000',
    })
    add_operation(db, user_id, account_id, holding_id, 'deposit', 500, f'{YEAR}-02-10')
    client.post('/investments/evolution/add', data={
        'holding_id': holding_id, 'valuation_date': f'{YEAR}-02-28', 'total_value': '1510',
    })

    # 1510 - (1000 + 500 deposited) = 10, not 510.
    assert valuation_returns(db, holding_id) == [pytest.approx(0), pytest.approx(10)]


def test_fixed_income_evolution_uses_current_position(client, db, user_id):
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'Fixed Income Fund', 20, 100, 100)

    client.post('/investments/evolution/add', data={
        'holding_id': holding_id, 'valuation_date': f'{YEAR}-03-01', 'return_pct': '1',
    })

    price = db.execute('SELECT current_price FROM investment_holdings WHERE id = ?', (holding_id,)).fetchone()[0]
    assert price == pytest.approx(101)
    item = dashboard_item(db, user_id, holding_id)
    # 20 units * +1 = 20, counted once (it already lives in the price).
    assert item['total_return_abs_base'] == pytest.approx(20)


def test_ytd_uses_last_valuation_before_this_year(client, db, user_id):
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'Mintos Lending', 1, 1000, 1100)
    db.execute(
        '''INSERT INTO investment_valuations (user_id, holding_id, valuation_date, total_value, currency, return_amount)
           VALUES (?, ?, ?, 1050, 'EUR', 50)''',
        (user_id, holding_id, f'{YEAR - 1}-12-31'),
    )
    db.commit()
    add_operation(db, user_id, account_id, holding_id, 'deposit', 30, f'{YEAR}-03-01')

    item = dashboard_item(db, user_id, holding_id)
    # 1100 now - (1050 at year start + 30 deposited this year) = 20
    assert item['ytd_return_base'] == pytest.approx(20)


def test_migration_drops_only_synthetic_dividends(app, db, user_id):
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'ETF', 1, 100, 100)
    add_operation(db, user_id, account_id, holding_id, 'dividend', 5, f'{YEAR}-01-01',
                  note='Auto return from valuation. imported')
    add_operation(db, user_id, account_id, holding_id, 'dividend', 7, f'{YEAR}-01-02', note='Real payout')
    db.execute('PRAGMA user_version = 2')
    db.commit()

    ft.run_migrations(db)

    notes = [row[0] for row in db.execute("SELECT note FROM investment_operations WHERE operation_type = 'dividend'")]
    assert notes == ['Real payout']
    assert db.execute('PRAGMA user_version').fetchone()[0] == len(ft.MIGRATIONS)


def test_back_dated_buy_keeps_newer_price(client, db, user_id):
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'ETF', 1, 100, 100)
    add_operation(db, user_id, account_id, holding_id, 'buy', 100, f'{YEAR}-05-01')
    db.execute('UPDATE investment_holdings SET current_price = 150 WHERE id = ?', (holding_id,))
    db.commit()

    client.post('/investments/operations/add', data={
        'account_id': account_id, 'holding_id': holding_id, 'operation_type': 'buy',
        'quantity': '1', 'price_per_unit': '90', 'amount': '90', 'currency': 'EUR',
        'operation_date': f'{YEAR}-01-15',
    })

    row = db.execute('SELECT quantity, avg_cost, current_price FROM investment_holdings WHERE id = ?',
                     (holding_id,)).fetchone()
    assert row['quantity'] == pytest.approx(2)
    assert row['avg_cost'] == pytest.approx(95)
    assert row['current_price'] == pytest.approx(150)


def test_sell_in_other_currency_realizes_gain_in_trade_currency(client, db, user_id):
    client.get('/dashboard')  # loads the (fake) ECB rates: 1 USD = 0.9 EUR
    account_id = make_investment_account(db, user_id)
    holding_id = make_holding(db, user_id, account_id, 'ETF', 10, 90, 90, currency='EUR')

    client.post('/investments/operations/add', data={
        'account_id': account_id, 'holding_id': holding_id, 'operation_type': 'sell',
        'quantity': '1', 'price_per_unit': '110', 'amount': '110', 'currency': 'USD',
        'operation_date': f'{YEAR}-06-01',
    })

    # Cost 90 EUR = 100 USD, sold at 110 USD -> +10 USD realized.
    gain, currency = db.execute(
        "SELECT realized_gain, currency FROM investment_operations WHERE operation_type = 'sell'"
    ).fetchone()
    assert (gain, currency) == (pytest.approx(10), 'USD')


def test_mintos_latest_statement_is_chosen_by_date_not_name(app, db, user_id, tmp_path):
    folder = tmp_path / 'raw' / 'mintos'
    header = ['Date', 'Transaction Type', 'Amount', 'Currency', 'Date from']
    write_xlsx(folder / 'Mintos 22_06_2026.xlsx', [
        header,
        [datetime(2026, 1, 5), 'Deposits', 100, 'EUR', datetime(2026, 1, 1)],
        [datetime(2026, 6, 20), 'Interest received', 1, 'EUR', None],
    ])
    write_xlsx(folder / 'Mintos 05_08_2026.xlsx', [
        header,
        [datetime(2026, 1, 5), 'Deposits', 100, 'EUR', datetime(2026, 1, 1)],
        [datetime(2026, 8, 1), 'Interest received', 3, 'EUR', None],
    ])
    account_id = make_investment_account(db, user_id)

    plan = ft.build_mintos_import_plan(db, user_id, account_id, None, True)

    assert plan['statement_end_date'] == '2026-08-01'
    assert plan['current_total_value'] == Decimal('103.00')
