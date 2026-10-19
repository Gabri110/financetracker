"""Sync page: connector output through the parsers, the saved-settings import and the
/sync routes. Connectors are stubbed, so nothing here touches the network or pytr."""
import html
import io
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from werkzeug.utils import secure_filename

import app as ft
import sync_connectors
from sync_connectors import SyncError
from tests.helpers import csv_text, make_account, write_xlsx

IBAN_A = 'ES1100000000000000000001'
IBAN_B = 'ES2200000000000000000002'
IBAN_C = 'ES3300000000000000000003'
CUSTOM_CATEGORY = '0f8fad5b-d9cb-469f-a165-70867728950e'
UNNAMED_CATEGORY = '7c9e6679-7425-40de-944b-e07fc1f90ae7'
REVOLUT_CURL = (
    "curl 'https://app.revolut.com/api/retail/user/current/transactions/last?count=50&walletId=w-1' \\\n"
    "  -H 'accept: application/json' \\\n"
    "  -H 'x-device-id: device-1' \\\n"
    "  -H 'accept-encoding: br, zstd' \\\n"
    "  -b 'token=secret'"
)
CONSENT = {
    'session_id': 's-1',
    'valid_until': '2027-01-01T00:00:00Z',
    'accounts': [{'uid': 'uid-a', 'iban': IBAN_A, 'name': 'Cuenta', 'currency': 'EUR'}],
}


# --- shared helpers ---------------------------------------------------------

def save_setting(db, user_id, key, value):
    ft.set_user_setting(db, user_id, key, value)
    db.commit()


def setting(db, user_id, key):
    return ft.get_user_setting(db, user_id, key)


def page_text(response):
    return html.unescape(response.get_data(as_text=True))


def folder_files(source):
    folder = ft.BANK_IMPORT_SOURCES[source]['folder']
    return sorted(name for name in os.listdir(folder) if os.path.isfile(os.path.join(folder, name)))


def write_source_file(source, name, text):
    with open(os.path.join(ft.BANK_IMPORT_SOURCES[source]['folder'], name), 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)


def transaction_rows(db):
    return [tuple(row) for row in db.execute(
        'SELECT id_transaction, account_id, transaction_type, amount FROM transactions ORDER BY id_transaction'
    )]


def epoch_ms(*args):
    # Midday local time, so the ISO date written by the connector is the same day.
    return int(datetime(*args).timestamp() * 1000)


def revolut_tx(leg_id, amount, day, **extra):
    return {
        'id': f'id-{leg_id}', 'legId': leg_id, 'type': 'CARD_PAYMENT', 'state': 'COMPLETED',
        'createdDate': epoch_ms(2026, 3, day, 12), 'completedDate': epoch_ms(2026, 3, day, 12),
        'currency': 'EUR', 'amount': amount, 'description': f'Shop {leg_id}', **extra,
    }


def eb_transaction(ref, amount, indicator, booking_date='2026-03-02', status='BOOK', **extra):
    return {
        'entry_reference': ref, 'transaction_amount': {'amount': amount, 'currency': 'EUR'},
        'credit_debit_indicator': indicator, 'booking_date': booking_date, 'status': status, **extra,
    }


def eb_account(uid, iban):
    return {'uid': uid, 'iban': iban, 'currency': 'EUR'}


def open_banking_csv(*accounts_and_transactions):
    rows = []
    for account, transactions in accounts_and_transactions:
        rows += sync_connectors.open_banking_transactions_to_rows(account, transactions)
    return sync_connectors.rows_to_csv_text(sync_connectors.OPEN_BANKING_CSV_FIELDS, rows)


def tr_csv(ids, start=date(2026, 2, 1)):
    """Trade Republic card payments in the official-export columns pytr output is mapped to."""
    rows = []
    for n, id_transaction in enumerate(ids):
        day = (start + timedelta(days=n)).isoformat()
        rows.append({
            'datetime': f'{day}T10:00:00', 'date': day, 'category': 'CASH', 'type': 'CARD_TRANSACTION',
            'amount': f'-{10 + n}.00', 'currency': 'EUR', 'description': f'Card {n}',
            'id_transaction': id_transaction,
        })
    return sync_connectors.rows_to_csv_text(sync_connectors.TRADE_REPUBLIC_CSV_FIELDS, rows)


def import_with_settings(db, user_id, source, settings):
    assert ft.validate_bank_import_settings(db, user_id, source, settings) is None
    return ft.run_bank_import(db, user_id, source, settings)


# --- stubbed connectors -----------------------------------------------------

@pytest.fixture
def revolut(monkeypatch):
    fake = SimpleNamespace(transactions=[], calls=[])

    def fetch(session, since):
        fake.calls.append({'session': session, 'since': since})
        return list(fake.transactions)

    monkeypatch.setattr(sync_connectors, 'fetch_revolut_transactions', fetch)
    return fake


@pytest.fixture
def trade_republic(monkeypatch):
    fake = SimpleNamespace(csv='')
    monkeypatch.setattr(sync_connectors, 'fetch_trade_republic_events', lambda since, cache_dir: [{}])
    monkeypatch.setattr(sync_connectors, 'trade_republic_events_to_csv', lambda events: (fake.csv, {}))
    return fake


class FakeEnableBanking:
    def __init__(self):
        self.transactions = {}
        self.authorizations = []

    def find_aspsp(self, name, country):
        return {'name': name, 'country': country}

    def start_authorization(self, aspsp, redirect_url, state):
        self.authorizations.append({'aspsp': aspsp, 'redirect_url': redirect_url, 'state': state})
        return f'https://bank.example/auth?state={state}'

    def create_session(self, code):
        if code != 'good-code':
            raise SyncError('Invalid authorization code.')
        return CONSENT

    def fetch_transactions(self, account_uid, date_from):
        return self.transactions.get(account_uid, [])

    def fetch_balance(self, account_uid):
        return {'amount': '975.00', 'currency': 'EUR'}


@pytest.fixture
def enable_banking(monkeypatch):
    fake = FakeEnableBanking()
    monkeypatch.setattr(ft, 'enable_banking_client', lambda: fake)
    monkeypatch.delenv('ENABLE_BANKING_REDIRECT_URL', raising=False)
    return fake


# --- parsers and connector output --------------------------------------------

MYINVESTOR_HEADERS = ['Fecha de operación', 'Fecha de valor', 'Concepto', 'Importe', 'Divisa']


def test_parse_myinvestor_csv():
    text = csv_text(MYINVESTOR_HEADERS, [
        ['02/01/2026', '02/01/2026', 'Transferencia desde Revolut', '1.234,56', 'EUR'],
        ['31/01/2026', '01/02/2026', 'Intereses', '0,42', 'EUR'],
        ['05/02/2026', '05/02/2026', 'Traspaso a cuenta', '-500,00', 'EUR'],
        ['05/02/2026', '05/02/2026', 'Traspaso a cuenta', '-500,00', 'EUR'],
        ['', '06/02/2026', 'Sin fecha', '1,00', 'EUR'],
        ['07/02/2026', '07/02/2026', 'Importe roto', 'n/a', 'EUR'],
    ], delimiter=';')
    result = ft.parse_myinvestor_csv(text)
    rows = result['rows']

    assert [(row['date'], row['amount']) for row in rows] == [
        ('2026-01-02', Decimal('1234.56')),
        ('2026-01-31', Decimal('0.42')),
        ('2026-02-05', Decimal('-500.00')),
        ('2026-02-05', Decimal('-500.00')),
    ]
    # The account only moved money between own accounts: interest included, all transfers.
    assert {row['transaction_type'] for row in rows} == {'transfer'}
    assert result['skipped'] == {'NO_DATE': 1, 'BAD_AMOUNT': 1}
    ids = [row['id_transaction'] for row in rows]
    assert len(set(ids)) == 4 and all(id_.startswith('myi_') for id_ in ids)
    # Same file, same ids: re-imports update rather than duplicate.
    assert [row['id_transaction'] for row in ft.parse_myinvestor_csv(text)['rows']] == ids


def test_open_banking_rows_round_trip_through_parser():
    text = open_banking_csv((eb_account('uid-a', IBAN_A), [
        eb_transaction('e1', '1500.00', 'CRDT', debtor={'name': 'Employer SL'},
                       remittance_information=['NOMINA', 'MARZO']),
        eb_transaction('e2', '42.10', 'DBIT', creditor={'name': 'Supermercado'},
                       remittance_information='COMPRA TARJETA'),
        eb_transaction('e3', '9.99', 'DBIT', status='PDNG'),
        eb_transaction('', '5.00', 'DBIT', remittance_information=['CUOTA']),
        eb_transaction('', '5.00', 'DBIT', remittance_information=['CUOTA']),
    ]))
    rows = ft.parse_open_banking_csv(text, 'bki')['rows']

    # The pending booking is not exported; debits come back negative.
    assert [(row['amount'], row['transaction_type']) for row in rows] == [
        (Decimal('1500.00'), 'income'),
        (Decimal('-42.10'), 'expense'),
        (Decimal('-5.00'), 'expense'),
        (Decimal('-5.00'), 'expense'),
    ]
    assert [row['id_transaction'] for row in rows[:2]] == ['bki_e1', 'bki_e2']
    # Credits name the debtor, debits the creditor.
    assert [row['payee'] for row in rows[:2]] == ['Employer SL', 'Supermercado']
    assert [row['description'] for row in rows[:2]] == ['NOMINA | MARZO', 'COMPRA TARJETA']
    assert {row['mapping_key'] for row in rows} == {IBAN_A}
    # Without an entry reference the id hashes the content: distinct per occurrence, stable.
    hashed = [row['id_transaction'] for row in rows[2:]]
    assert hashed[0] != hashed[1] and all(id_.startswith('bki_') for id_ in hashed)
    assert [row['id_transaction'] for row in ft.parse_open_banking_csv(text, 'bki')['rows'][2:]] == hashed


def test_parse_open_banking_csv_skips_bad_rows():
    text = csv_text(sync_connectors.OPEN_BANKING_CSV_FIELDS, [
        [IBAN_A, 'u', 'x1', '', '', '-1.00', 'EUR', 'no date', '', 'BOOK'],
        [IBAN_A, 'u', 'x2', '2026-03-02', '', 'abc', 'EUR', 'bad amount', '', 'BOOK'],
        [IBAN_A.lower(), 'u', 'x3', '2026-03-02T10:00:00', '', '-1.00', 'EUR', 'ok', '', 'BOOK'],
    ])
    result = ft.parse_open_banking_csv(text, 'ing')

    assert result['skipped'] == {'NO_DATE': 1, 'BAD_AMOUNT': 1}
    [row] = result['rows']
    assert (row['id_transaction'], row['date'], row['mapping_key']) == ('ing_x3', '2026-03-02', IBAN_A)


def test_revolut_transactions_round_trip_through_parser():
    text, unknown = sync_connectors.revolut_transactions_to_csv([
        revolut_tx('l1', -350, 2, merchant={'name': 'Cafe'}, category=CUSTOM_CATEGORY.upper()),
        revolut_tx('l2', -1200, 3, category=UNNAMED_CATEGORY),
        revolut_tx('l3', 5000, 4, type='TOPUP', state='PENDING'),
    ], {CUSTOM_CATEGORY: 'Coffee'})
    result = ft.parse_revolut_csv(text)
    rows = {row['id_transaction']: row for row in result['rows']}

    assert unknown == [UNNAMED_CATEGORY]
    assert result['skipped'] == {'PENDING': 1}
    assert (rows['rev_l1']['date'], rows['rev_l1']['amount']) == ('2026-03-02', Decimal('-3.50'))
    assert (rows['rev_l1']['category_text'], rows['rev_l1']['payee']) == ('Coffee', 'Cafe')
    assert rows['rev_l2']['category_text'] == ''


def test_parse_revolut_curl():
    session = sync_connectors.parse_revolut_curl(REVOLUT_CURL)

    assert (session['cookie'], session['device_id'], session['wallet_id']) == ('token=secret', 'device-1', 'w-1')
    # Headers requests sets itself (or can't decode) are not replayed.
    assert 'accept-encoding' not in session['headers']
    assert session['headers']['accept'] == 'application/json'


@pytest.mark.parametrize('curl, message', [
    ("curl 'https://example.com/x?walletId=w' -H 'x-device-id: d' -b 'c=1'", 'not a cURL'),
    ("curl 'https://app.revolut.com/api/x?walletId=w' -H 'x-device-id: d'", 'no session cookie'),
    ("curl 'https://app.revolut.com/api/x' -H 'x-device-id: d' -b 'c=1'", 'no walletId'),
])
def test_parse_revolut_curl_rejects_incomplete_requests(curl, message):
    with pytest.raises(SyncError, match=message):
        sync_connectors.parse_revolut_curl(curl)


def test_parse_revolver_custom_categories():
    text = (
        f'[general]\n{CUSTOM_CATEGORY} = Wrong section\n'
        f'[custom.categories]\n# a comment\n{CUSTOM_CATEGORY.upper()} = Coffee\n'
        f'not-a-uuid = Ignored\n{UNNAMED_CATEGORY} =\n'
    )
    assert sync_connectors.parse_revolver_custom_categories(text) == {CUSTOM_CATEGORY: 'Coffee'}


# --- import with saved settings ----------------------------------------------

def test_open_banking_import_maps_rows_by_iban_and_is_idempotent(app, db, user_id):
    main = make_account(db, user_id, 'Bankinter main')
    savings = make_account(db, user_id, 'Bankinter savings', account_type='savings')
    write_source_file('bankinter', '2026_03_10_api.csv', open_banking_csv(
        (eb_account('uid-a', IBAN_A), [eb_transaction('a1', '1000.00', 'CRDT'), eb_transaction('a2', '25.00', 'DBIT')]),
        (eb_account('uid-b', IBAN_B), [eb_transaction('b1', '50.00', 'CRDT')]),
        (eb_account('uid-c', IBAN_C), [eb_transaction('c1', '7.00', 'DBIT')]),
    ))
    # As saved by the Bank Import form: ids as strings, an IBAN left unmapped.
    settings = {
        'currency_accounts': {IBAN_A.lower(): str(main), IBAN_B: savings, IBAN_C: ''},
        'enabled_currencies': [IBAN_A, IBAN_B, IBAN_C],
    }

    first = import_with_settings(db, user_id, 'bankinter', settings)
    second = import_with_settings(db, user_id, 'bankinter', settings)

    assert (first['imported'], first['updated'], first['errors']) == (3, 0, [])
    assert (second['imported'], second['updated']) == (0, 3)
    assert transaction_rows(db) == [
        ('bki_a1', main, 'income', 1000.0),
        ('bki_a2', main, 'expense', 25.0),
        ('bki_b1', savings, 'income', 50.0),
    ]
    saved = setting(db, user_id, 'bank_import.bankinter')
    assert saved['currency_accounts'] == {IBAN_A: main, IBAN_B: savings}


# --- /sync routes ----------------------------------------------------------------

def test_revolut_sync_needs_one_manual_import_then_imports(client, db, user_id, revolut):
    revolut.transactions = [revolut_tx('l1', -350, 2), revolut_tx('l2', -1200, 3)]

    page = page_text(client.post('/sync/revolut', data={'curl': REVOLUT_CURL}, follow_redirects=True))
    assert 'Map its accounts once on Bank Import' in page
    assert len(folder_files('revolut')) == 1  # the fetched file waits in the drop folder
    assert setting(db, user_id, 'sync.revolut')['result'] == 'needs_setup'
    assert transaction_rows(db) == []
    assert revolut.calls[0]['session']['wallet_id'] == 'w-1'
    assert revolut.calls[0]['since'] == date.today() - timedelta(days=730)

    account_id = make_account(db, user_id, 'Revolut EUR')
    save_setting(db, user_id, 'bank_import.revolut',
                 {'currency_accounts': {'EUR': account_id}, 'enabled_currencies': ['EUR']})
    page = page_text(client.post('/sync/revolut', data={'curl': REVOLUT_CURL}, follow_redirects=True))
    assert 'Revolut: imported 2 new, updated 0 existing' in page
    status = setting(db, user_id, 'sync.revolut')
    assert (status['result'], status['imported'], status['fetched']) == ('imported', 2, 2)

    # The next sync starts a month before the newest imported row and adds no duplicates.
    page = page_text(client.post('/sync/revolut', data={'curl': REVOLUT_CURL}, follow_redirects=True))
    assert revolut.calls[-1]['since'] == date(2026, 3, 3) - timedelta(days=ft.SYNC_FETCH_OVERLAP_DAYS)
    assert 'Revolut: imported 0 new, updated 2 existing' in page
    assert [row[0] for row in transaction_rows(db)] == ['rev_l1', 'rev_l2']


def test_revolut_sync_reports_connector_errors(client, monkeypatch):
    page = page_text(client.post('/sync/revolut', data={'curl': 'curl https://example.com'}, follow_redirects=True))
    assert 'Revolut: That is not a cURL of an app.revolut.com request.' in page

    def expired(session, since):
        raise SyncError('The Revolut session expired.')

    monkeypatch.setattr(sync_connectors, 'fetch_revolut_transactions', expired)
    response = client.post('/sync/revolut', data={'curl': REVOLUT_CURL}, follow_redirects=True)
    assert response.status_code == 200
    assert 'Revolut: The Revolut session expired.' in page_text(response)


def test_revolut_categories_are_saved(client, db, user_id):
    client.post('/sync/revolut/categories', data={'categories': f'{CUSTOM_CATEGORY} = Coffee'})
    assert setting(db, user_id, 'revolut.custom_categories') == {CUSTOM_CATEGORY: 'Coffee'}


def test_trade_republic_sync_refuses_ids_that_do_not_match_the_export(client, db, user_id, trade_republic):
    account_id = make_account(db, user_id, 'Trade Republic')
    exported_ids = [f'exp{n}' for n in range(6)]
    write_source_file('trade_republic', '2026_02_10_export.csv', tr_csv(exported_ids))
    import_with_settings(db, user_id, 'trade_republic', {'account_id': account_id})

    # pytr returns other ids for the same period: importing would duplicate every row.
    trade_republic.csv = tr_csv([f'pytr{n}' for n in range(6)])
    page = page_text(client.post('/sync/trade_republic', follow_redirects=True))
    assert 'only 0 of 6 fetched rows' in page and 'Nothing imported' in page
    assert len(transaction_rows(db)) == 6
    assert folder_files('trade_republic') == ['2026_02_10_export.csv']  # parked in pytr_cache/ instead

    trade_republic.csv = tr_csv(exported_ids + ['new1'])
    page = page_text(client.post('/sync/trade_republic', follow_redirects=True))
    assert 'Trade Republic: imported 1 new, updated 6 existing' in page
    assert len(transaction_rows(db)) == 7


def test_bank_connection_checks_the_oauth_state(client, db, user_id, enable_banking):
    response = client.post('/sync/bank/bankinter/connect')
    assert response.status_code == 302
    assert response.headers['Location'].startswith('https://bank.example/auth')
    [authorization] = enable_banking.authorizations
    assert authorization['aspsp'] == {'name': 'Bankinter', 'country': 'ES'}
    assert authorization['redirect_url'] == 'http://localhost/sync/callback'

    # A callback with another state (a link that didn't start here) is refused and
    # uses up the pending state, so the original link can't be replayed either.
    page = page_text(client.get('/sync/callback?code=good-code&state=forged', follow_redirects=True))
    assert 'did not start here' in page
    page = page_text(client.get(
        f"/sync/callback?code=good-code&state={authorization['state']}", follow_redirects=True
    ))
    assert 'did not start here' in page
    assert setting(db, user_id, 'enable_banking.bankinter') is None

    client.post('/sync/bank/bankinter/connect')
    state = enable_banking.authorizations[-1]['state']
    page = page_text(client.get(f'/sync/callback?code=good-code&state={state}', follow_redirects=True))
    assert 'Bankinter: connected 1 account(s), consent valid until 2027-01-01' in page
    assert setting(db, user_id, 'enable_banking.bankinter') == CONSENT


def test_bank_connection_manual_callback_and_cancel(client, db, user_id, enable_banking):
    client.post('/sync/bank/ing/connect')
    state = enable_banking.authorizations[-1]['state']
    page = page_text(client.post('/sync/callback/manual', data={
        'redirected_url': f'https://my.example/landing?code=good-code&state={state}',
    }, follow_redirects=True))
    assert 'ING: connected 1 account(s)' in page
    assert setting(db, user_id, 'enable_banking.ing') == CONSENT

    client.post('/sync/bank/bankinter/connect')
    page = page_text(client.get(
        '/sync/callback?error=access_denied&error_description=User+cancelled', follow_redirects=True
    ))
    assert 'Bank connection cancelled: User cancelled' in page
    assert setting(db, user_id, 'enable_banking.bankinter') is None


@pytest.mark.parametrize('path', ['/sync/bank/b100/connect', '/sync/bank/revolut', '/sync/bank/nope'])
def test_bank_routes_only_accept_open_banking_sources(client, enable_banking, path):
    assert 'Unknown bank.' in page_text(client.post(path, follow_redirects=True))
    assert enable_banking.authorizations == []


def test_sync_all_runs_every_connected_source(client, db, user_id, revolut, enable_banking, monkeypatch):
    revolut_account = make_account(db, user_id, 'Revolut EUR')
    bank_account = make_account(db, user_id, 'Bankinter')
    save_setting(db, user_id, 'bank_import.revolut',
                 {'currency_accounts': {'EUR': revolut_account}, 'enabled_currencies': ['EUR']})
    save_setting(db, user_id, 'bank_import.bankinter',
                 {'currency_accounts': {IBAN_A: bank_account}, 'enabled_currencies': [IBAN_A]})
    save_setting(db, user_id, 'enable_banking.bankinter', CONSENT)
    revolut.transactions = [revolut_tx('l1', -350, 2)]
    enable_banking.transactions['uid-a'] = [eb_transaction('a1', '1000.00', 'CRDT'), eb_transaction('a2', '25.00', 'DBIT')]
    trade_republic_runs = []
    monkeypatch.setattr(ft, 'sync_trade_republic', lambda *args: trade_republic_runs.append(args) or [])

    page = page_text(client.post('/sync/all', data={'curl': REVOLUT_CURL}, follow_redirects=True))

    assert 'Revolut: imported 1 new' in page and 'Bankinter: imported 2 new' in page
    assert trade_republic_runs == []  # never imported manually, so not synced
    assert setting(db, user_id, 'sync.ing') is None  # not connected
    assert transaction_rows(db) == [
        ('bki_a1', bank_account, 'income', 1000.0),
        ('bki_a2', bank_account, 'expense', 25.0),
        ('rev_l1', revolut_account, 'expense', 3.5),
    ]
    assert setting(db, user_id, 'sync.bankinter')['balances'] == [
        {'account_key': IBAN_A, 'amount': '975.00', 'currency': 'EUR'},
    ]
    assert client.get('/sync').status_code == 200  # renders the balance check


def test_sync_all_with_nothing_connected(client):
    assert 'Nothing to sync yet' in page_text(client.post('/sync/all', follow_redirects=True))


EDENRED_NAME = 'Edenred Movimientos Cuenta Restaurante 20260101.xlsx'


def test_upload_saves_into_source_folder_and_imports(client, db, user_id, tmp_path):
    account_id = make_account(db, user_id, 'Edenred')
    save_setting(db, user_id, 'bank_import.edenred_restaurant', {'account_id': account_id})
    path = write_xlsx(tmp_path / EDENRED_NAME, [
        ['Movimientos'],
        ['Fecha', 'Detalle', 'Importe'],
        [datetime(2026, 1, 2, 9, 0), 'RECARGA', 100],
        [datetime(2026, 1, 3, 13, 0), 'Bar Pepe - Madrid', 12.5],
    ])
    with open(path, 'rb') as handle:
        payload = handle.read()

    page = page_text(client.post('/sync/upload/edenred_restaurant', data={
        'file': (io.BytesIO(payload), EDENRED_NAME),
    }, content_type='multipart/form-data', follow_redirects=True))

    assert 'Edenred Restaurante: imported 2 new' in page
    assert folder_files('edenred_restaurant') == [secure_filename(EDENRED_NAME)]
    assert sorted(row[1:] for row in transaction_rows(db)) == [(account_id, 'expense', 12.5), (account_id, 'income', 100.0)]


@pytest.mark.parametrize('source, filename, message', [
    ('b100', 'old.csv', 'Unknown upload source.'),  # archived
    ('myinvestor', 'old.csv', 'Unknown upload source.'),  # archived
    ('revolut', 'r.csv', 'Unknown upload source.'),  # fetched, not uploaded
    ('bankinter', 'b.csv', 'Unknown upload source.'),  # fetched, not uploaded
    ('edenred_restaurant', 'statement.csv', 'Only .xlsx files can be added here.'),
])
def test_upload_rejects_other_sources_and_extensions(client, source, filename, message):
    page = page_text(client.post(f'/sync/upload/{source}', data={
        'file': (io.BytesIO(b'data'), filename),
    }, content_type='multipart/form-data', follow_redirects=True))
    assert message in page
    assert folder_files(source) == []


def test_archived_sources_stay_off_the_sync_page(client, db, user_id):
    sources, _, _ = ft.build_sync_context(db, user_id)
    keys = {source['key'] for source in sources}
    assert {'revolut', 'trade_republic', 'bankinter', 'ing', 'edenred_restaurant'} <= keys
    assert keys.isdisjoint({'b100', 'iberia_cards', 'myinvestor'})

    # ...but their historic exports can still be imported from Bank Import.
    make_account(db, user_id)  # the page lists sources once an account exists
    page = page_text(client.get('/import/bank'))
    assert 'Archived sources (3)' in page
    assert all(label in page for label in ('B100', 'Iberia Cards', 'MyInvestor'))


@pytest.mark.parametrize('path', [
    '/sync/all', '/sync/revolut', '/sync/revolut/categories', '/sync/trade_republic/login',
    '/sync/bank/bankinter/connect', '/sync/bank/bankinter', '/sync/callback/manual',
    '/sync/upload/edenred_restaurant',
])
def test_sync_posts_reject_cross_site_requests(client, path):
    assert client.post(path, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post(path, headers={'Referer': 'https://evil.example/page'}).status_code == 403


def test_sync_posts_accept_same_site_requests(client):
    response = client.post('/sync/all', headers={'Origin': 'http://localhost'})
    assert response.status_code == 302
