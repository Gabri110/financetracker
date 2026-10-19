"""
Connectors for the Sync page: fetch data from Revolut, Trade Republic and
open-banking banks (Enable Banking), and render it as CSV in the format the
matching parser in app.py already reads. No Flask imports; app.py writes the
CSV into data/raw/<source>/ and runs the normal bank import on it.

Each connector raises SyncError with a message meant for the user.
"""

import asyncio
import calendar
import csv
import io
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import requests

HTTP_TIMEOUT = 20


class SyncError(Exception):
    """A failure the user can act on (expired token, missing login, ...).
    `action` hints the UI what to offer next ('login', 'reconnect')."""

    def __init__(self, message, action=None):
        super().__init__(message)
        self.action = action


def rows_to_csv_text(fieldnames, rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: '' if row.get(key) is None else row.get(key) for key in fieldnames})
    return output.getvalue()


def month_end_epochs_ms(start, end):
    """Epoch (ms) of the last second of every month from start to end (dates)."""
    epochs = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        last_day = calendar.monthrange(year, month)[1]
        epochs.append(int(datetime(year, month, last_day, 23, 59, 59).timestamp()) * 1000)
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return epochs


# ---------------------------------------------------------------------------
# Revolut: replays the request the web app (app.revolut.com) makes, using the
# session from a "Copy as cURL" of the transactions/last request. Ported from
# the RevolVer script. The cURL is used for one fetch and never stored.
# ---------------------------------------------------------------------------

REVOLUT_TRANSACTIONS_URL = 'https://app.revolut.com/api/retail/user/current/transactions/last'
REVOLUT_CSV_FIELDS = [
    'id', 'legId', 'type', 'state', 'startedDate', 'updatedDate', 'completedDate', 'createdDate',
    'currency', 'amount', 'fee', 'balance', 'amountWithCharges', 'description', 'reason', 'tag',
    'category', 'relatedTransactionId', 'account_id', 'account_type', 'countryCode', 'rate',
    'merchant_category', 'merchant_name', 'merchant_mcc', 'merchant_scheme', 'merchant_city',
    'merchant_country', 'merchant_state', 'merchant_postcode', 'merchant_address',
    'counterpart_amount', 'counterpart_currency', 'comment', 'cardLastFour', 'cardLabel',
]
# Headers that must not be replayed: requests sets them itself, and a copied
# 'accept-encoding: br, zstd' would return a body requests can't decode.
REVOLUT_SKIPPED_HEADERS = {'cookie', 'content-length', 'accept-encoding', 'host', 'connection'}
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def parse_revolut_curl(curl_text):
    """Extract the session from a 'Copy as cURL' (bash or cmd) of the Revolut
    transactions/last request."""
    text = (curl_text or '').strip()
    # Windows "Copy as cURL (cmd)" escapes quotes and continues lines with ^.
    text = text.replace('^\\^"', '"').replace('^"', "'").replace(' ^\n', ' \n').replace('^\n', '\n')
    text = text.replace('\\\n', ' ').replace('\n', ' ')

    url_match = re.search(r"curl\s+(?:--url\s+)?['\"]([^'\"]+)", text)
    if not url_match or 'revolut.com' not in url_match.group(1):
        raise SyncError('That is not a cURL of an app.revolut.com request.')
    query = parse_qs(urlparse(url_match.group(1)).query)

    headers = {}
    for name, value in re.findall(r"(?:-H|--header)\s+'([^':]+):\s*([^']*)'", text):
        headers[name.strip().lower()] = value.strip()
    for name, value in re.findall(r'(?:-H|--header)\s+"([^":]+):\s*([^"]*)"', text):
        headers.setdefault(name.strip().lower(), value.strip())

    cookie_match = re.search(r"(?:-b|--cookie)\s+'([^']+)'", text) or re.search(r'(?:-b|--cookie)\s+"([^"]+)"', text)
    cookie = cookie_match.group(1) if cookie_match else headers.get('cookie', '')

    session = {
        'cookie': cookie,
        'device_id': headers.get('x-device-id', ''),
        'wallet_id': (query.get('walletId') or [''])[0],
        'pocket_id': (query.get('internalPocketId') or [''])[0],
        'headers': {k: v for k, v in headers.items() if k not in REVOLUT_SKIPPED_HEADERS},
    }
    if not session['cookie'] or not session['device_id']:
        raise SyncError('The cURL has no session cookie or x-device-id. Copy it again from the transactions/last request.')
    if not session['wallet_id'] and not session['pocket_id']:
        raise SyncError('The cURL URL has no walletId. Copy the request made by "See all" transactions.')
    return session


def fetch_revolut_transactions(session, since):
    """All transactions from the month of `since` (a date) until now, deduped by legId."""
    headers = dict(session['headers'])
    headers['cookie'] = session['cookie']
    headers['x-device-id'] = session['device_id']
    params = {'count': 500}
    if session['wallet_id']:
        params['walletId'] = session['wallet_id']
    else:
        params['internalPocketId'] = session['pocket_id']

    by_leg = {}
    for epoch in month_end_epochs_ms(since, date.today()):
        try:
            response = requests.get(
                REVOLUT_TRANSACTIONS_URL,
                headers=headers,
                params={**params, 'to': epoch},
                timeout=HTTP_TIMEOUT,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SyncError(f'Revolut request failed: {exc}')
        if isinstance(payload, dict):
            if payload.get('code') == 9039 or response.status_code == 401:
                raise SyncError('The Revolut session expired. Copy a fresh cURL and try again right away.')
            raise SyncError(f"Revolut returned an error: {payload.get('message') or payload}")
        for transaction in payload:
            leg_id = transaction.get('legId') or transaction.get('id')
            if leg_id:
                by_leg[leg_id] = transaction
    return list(by_leg.values())


def _revolut_epoch_to_iso(value):
    if not value:
        return ''
    return datetime.fromtimestamp(value / 1000).isoformat()


def revolut_transactions_to_csv(transactions, custom_categories):
    """Flatten Revolut JSON to the RevolVer CSV columns. Custom categories come
    back as UUIDs; they're named via custom_categories ({uuid: name}).
    Returns (csv_text, unknown_category_ids)."""
    unknown = set()
    rows = []
    for t in transactions:
        merchant = t.get('merchant') or {}
        account = t.get('account') or {}
        counterpart = t.get('counterpart') or {}
        card = t.get('card') or {}
        category = t.get('category') or ''
        if UUID_RE.match(category):
            named = custom_categories.get(category.lower())
            if not named:
                unknown.add(category.lower())
            category = named or ''
        rows.append({
            'id': t.get('id'),
            'legId': t.get('legId'),
            'type': t.get('type'),
            'state': t.get('state'),
            'startedDate': _revolut_epoch_to_iso(t.get('startedDate')),
            'updatedDate': _revolut_epoch_to_iso(t.get('updatedDate')),
            'completedDate': _revolut_epoch_to_iso(t.get('completedDate')),
            'createdDate': _revolut_epoch_to_iso(t.get('createdDate')),
            'currency': t.get('currency'),
            'amount': t.get('amount'),
            'fee': t.get('fee'),
            'balance': t.get('balance'),
            'amountWithCharges': t.get('amountWithCharges'),
            'description': t.get('description'),
            'reason': t.get('reason'),
            'tag': t.get('tag'),
            'category': category,
            'relatedTransactionId': t.get('relatedTransactionId'),
            'account_id': account.get('id'),
            'account_type': account.get('type'),
            'countryCode': t.get('countryCode'),
            'rate': t.get('rate'),
            'merchant_category': merchant.get('category'),
            'merchant_name': merchant.get('name'),
            'merchant_mcc': merchant.get('mcc'),
            'merchant_scheme': merchant.get('scheme'),
            'merchant_city': merchant.get('city'),
            'merchant_country': merchant.get('country'),
            'merchant_state': merchant.get('state'),
            'merchant_postcode': merchant.get('postcode'),
            'merchant_address': merchant.get('address'),
            'counterpart_amount': counterpart.get('amount'),
            'counterpart_currency': counterpart.get('currency'),
            'comment': t.get('comment'),
            'cardLastFour': card.get('lastFour'),
            'cardLabel': card.get('label'),
        })
    rows.sort(key=lambda row: row['createdDate'] or '')
    return rows_to_csv_text(REVOLUT_CSV_FIELDS, rows), sorted(unknown)


def parse_revolver_custom_categories(ini_text):
    """{uuid: name} from the [custom.categories] section of RevolVer's config.ini."""
    mapping = {}
    in_section = False
    for line in (ini_text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('['):
            in_section = stripped.lower() == '[custom.categories]'
            continue
        if in_section and '=' in stripped and not stripped.startswith(('#', ';')):
            key, value = stripped.split('=', 1)
            if UUID_RE.match(key.strip()) and value.strip():
                mapping[key.strip().lower()] = value.strip()
    return mapping


# ---------------------------------------------------------------------------
# Trade Republic via pytr (unofficial private API). Login happens once with
# `pytr login --v2 --store_credentials` (phone + PIN stay in ~/.pytr, never in
# this app); afterwards the saved session cookies are reused, and an expired
# session is renewed by confirming a push in the Trade Republic app.
# Events are mapped to the columns of TR's official CSV export, so
# parse_trade_republic_csv handles both.
# ---------------------------------------------------------------------------

TRADE_REPUBLIC_CSV_FIELDS = [
    'datetime', 'date', 'category', 'type', 'asset_class', 'name', 'symbol', 'shares', 'price',
    'amount', 'fee', 'tax', 'currency', 'description', 'id_transaction', 'counterparty_name',
]
TR_CARD_EVENTS = {
    'CARD_TRANSACTION', 'CARD_SUCCESSFUL_TRANSACTION', 'CARD_SUCCESSFUL_ATM_WITHDRAWAL', 'CARD_ORDER_BILLED',
}
TR_CARD_REFUND_EVENTS = {'CARD_REFUND', 'CARD_TR_REFUND', 'CARD_SUCCESSFUL_OCT'}
TR_INBOUND_EVENTS = {
    'ACCOUNT_TRANSFER_INCOMING', 'BANK_TRANSACTION_INCOMING', 'INCOMING_TRANSFER', 'INCOMING_TRANSFER_DELEGATION',
    'PAYMENT_INBOUND', 'PAYMENT_INBOUND_APPLE_PAY', 'PAYMENT_INBOUND_CREDIT_CARD', 'PAYMENT_INBOUND_GOOGLE_PAY',
    'PAYMENT_INBOUND_SEPA_DIRECT_DEBIT', 'PAYMENT-SERVICE-IN-PAYMENT-DIRECT-DEBIT',
}
TR_OUTBOUND_EVENTS = {
    'BANK_TRANSACTION_OUTGOING', 'OUTGOING_TRANSFER', 'OUTGOING_TRANSFER_DELEGATION', 'PAYMENT_OUTBOUND',
    'JUNIOR_P2P_TRANSFER',
}
TR_INTEREST_EVENTS = {'INTEREST_PAYOUT', 'INTEREST_PAYOUT_CREATED'}
TR_SAVEBACK_EVENTS = {'ACQUISITION_TRADE_PERK', 'BENEFITS_SAVEBACK_EXECUTION', 'SAVEBACK_AGGREGATE'}
TR_TRADE_EVENTS = {
    'BENEFITS_SPARE_CHANGE_EXECUTION', 'IPO_TRADE_EXECUTED', 'ORDER_EXECUTED', 'SAVINGS_PLAN_EXECUTED',
    'SAVINGS_PLAN_INVOICE_CREATED', 'SPARE_CHANGE_AGGREGATE', 'TRADE_CORRECTED', 'TRADE_INVOICE',
    'TRADING_SAVINGSPLAN_EXECUTED', 'TRADING_TRADE_EXECUTED',
}
TR_PRIVATE_PREPAYMENT_EVENTS = {'PRIVATE_MARKETS_ORDER_CREATED'}
TR_PRIVATE_SETTLEMENT_EVENTS = {'PRIVATE_MARKET_FUND_TRADE_EXECUTED', 'PRIVATE_MARKETS_TRADE_EXECUTED'}
TR_DIVIDEND_EVENTS = {'CREDIT'}
TR_TAX_EVENTS = {'SSP_TAX_CORRECTION', 'SSP_TAX_CORRECTION_INVOICE', 'TAX_CORRECTION', 'TAX_REFUND'}
TR_SKIPPED_STATUSES = {'CANCELED', 'CANCELLED', 'FAILED', 'PENDING', 'REJECTED'}


def _import_pytr():
    try:
        from pytr.api import TradeRepublicApi
        from pytr.event import Event
        from pytr.timeline import Timeline
    except ImportError:
        raise SyncError('pytr is not installed. Run: pip install -r requirements.txt')
    return TradeRepublicApi, Event, Timeline


def _trade_republic_api():
    TradeRepublicApi, _, _ = _import_pytr()
    try:
        # locale 'de': pytr's event parser reads the German detail labels.
        return TradeRepublicApi(save_cookies=True, use_v2_login=True, locale='de')
    except ValueError:
        raise SyncError(
            'No Trade Republic credentials found. Run once in a terminal: '
            'pytr login --v2 --store_credentials'
        )


def trade_republic_login():
    """Start a v2 web login and block until it is confirmed in the TR app
    (pytr polls for ~2 minutes). Saves the session cookies for later syncs."""
    tr = _trade_republic_api()
    if tr.resume_websession():
        return 'already'
    try:
        tr.initiate_weblogin()
        if tr.weblogin_needs_authenticator:
            raise SyncError(
                'Your Trade Republic login needs an authenticator code. Run in a terminal: pytr login --v2'
            )
        tr.complete_weblogin()
    except TimeoutError:
        raise SyncError('The login was not confirmed in the Trade Republic app in time. Try again.')
    except (ValueError, requests.RequestException) as exc:
        raise SyncError(f'Trade Republic login failed: {exc}')
    return 'confirmed'


def fetch_trade_republic_events(since, cache_dir):
    """Raw timeline events (with details) newer than `since` (a date or None)."""
    _, _, Timeline = _import_pytr()
    tr = _trade_republic_api()
    if not tr.resume_websession():
        raise SyncError('The Trade Republic session expired.', 'login')
    # pytr keeps subscriptions on the class; drop leftovers from an earlier sync.
    tr.subscriptions.clear()
    not_before = datetime.combine(since, datetime.min.time()).timestamp() if since else 0.0
    timeline = Timeline(tr, cache_dir, not_before=not_before, store_event_database=False)
    try:
        asyncio.run(timeline.tl_loop())
    except Exception as exc:  # websocket / API changes surface as many types
        raise SyncError(f'Trade Republic fetch failed (pytr may need an update): {exc}')
    return timeline.events


def trade_republic_events_to_csv(raw_events):
    """Map pytr timeline events to TR official-export rows.
    Returns (csv_text, skipped_counts)."""
    _, Event, _ = _import_pytr()
    rows = []
    skipped = {}

    def skip(reason):
        skipped[reason] = skipped.get(reason, 0) + 1

    for raw in raw_events:
        event_type = (raw.get('eventType') or '').upper()
        if (raw.get('status') or '').upper() in TR_SKIPPED_STATUSES:
            skip(f"status {raw.get('status')}")
            continue
        try:
            event = Event.from_dict(raw)
        except Exception:
            skip(f'unparsed {event_type or "event"}')
            continue

        value = event.value if event.value is not None else (raw.get('amount') or {}).get('value')
        if value is None:
            skip(f'no amount {event_type}')
            continue
        timestamp = event.date.replace(tzinfo=None)
        base = {
            'datetime': timestamp.isoformat(timespec='seconds'),
            'date': timestamp.date().isoformat(),
            'currency': (raw.get('amount') or {}).get('currency') or 'EUR',
            'name': event.title,
            'description': ' - '.join(part for part in (raw.get('title'), raw.get('subtitle')) if part),
            'id_transaction': raw['id'],
            'fee': event.fees,
            'tax': event.taxes,
        }

        def trade_row(row_type, amount, asset_class='', **extra):
            shares = abs(event.shares) if event.shares else None
            price = None
            if shares and value:
                price = round((abs(value) - abs(event.fees or 0)) / shares, 6)
            return {
                **base, 'category': 'TRADING', 'type': row_type, 'amount': amount,
                'symbol': event.isin or '', 'shares': shares, 'price': price,
                'asset_class': asset_class, **extra,
            }

        if event_type in TR_CARD_EVENTS:
            rows.append({**base, 'category': 'CASH', 'type': 'CARD_TRANSACTION', 'amount': value})
        elif event_type in TR_CARD_REFUND_EVENTS:
            rows.append({**base, 'category': 'CASH', 'type': 'CARD_REFUND', 'amount': value})
        elif event_type in TR_INBOUND_EVENTS:
            rows.append({**base, 'category': 'CASH', 'type': 'TRANSFER_INBOUND', 'amount': value,
                         'counterparty_name': raw.get('subtitle') or ''})
        elif event_type in TR_OUTBOUND_EVENTS:
            rows.append({**base, 'category': 'CASH', 'type': 'TRANSFER_OUTBOUND', 'amount': value,
                         'counterparty_name': raw.get('subtitle') or ''})
        elif event_type in TR_INTEREST_EVENTS:
            rows.append({**base, 'category': 'CASH', 'type': 'INTEREST_PAYMENT', 'amount': value})
        elif event_type in TR_DIVIDEND_EVENTS:
            rows.append({**base, 'category': 'CASH', 'type': 'DIVIDEND', 'amount': value})
        elif event_type in TR_TAX_EVENTS:
            rows.append({**base, 'category': 'CASH', 'type': 'TAX_REFUND', 'amount': value})
        elif event_type in TR_SAVEBACK_EVENTS:
            # Same shape as the official export: a cash reward plus a buy.
            rows.append({**base, 'category': 'CASH', 'type': 'BENEFITS_SAVEBACK', 'amount': abs(value),
                         'id_transaction': f"{raw['id']}_reward"})
            rows.append(trade_row('BUY', -abs(value)))
        elif event_type in TR_TRADE_EVENTS:
            rows.append(trade_row('BUY' if value < 0 else 'SELL', value))
        elif event_type in TR_PRIVATE_PREPAYMENT_EVENTS:
            # Cash pre-payment; the units arrive with the settlement event.
            rows.append(trade_row('PRIVATE_MARKET_BUY', value, 'PRIVATE_FUND', shares=None, price=None,
                                  category='CASH'))
        elif event_type in TR_PRIVATE_SETTLEMENT_EVENTS:
            rows.append(trade_row('BUY', 0, 'PRIVATE_FUND'))
        else:
            skip(event_type or 'unknown event')

    rows.sort(key=lambda row: (row['datetime'], row['id_transaction']))
    return rows_to_csv_text(TRADE_REPUBLIC_CSV_FIELDS, rows), skipped


# ---------------------------------------------------------------------------
# Enable Banking (PSD2 aggregator, free "restricted production" mode for your
# own accounts). Auth: RS256 JWT signed with the application's private key.
# Docs: https://enablebanking.com/docs/api/reference/
# ---------------------------------------------------------------------------

ENABLE_BANKING_API = 'https://api.enablebanking.com'
ENABLE_BANKING_MAX_CONSENT_DAYS = 180
OPEN_BANKING_CSV_FIELDS = [
    'account_key', 'account_uid', 'id', 'booking_date', 'value_date', 'amount', 'currency',
    'description', 'payee', 'status',
]


class EnableBankingClient:
    def __init__(self, app_id, key_path):
        if not app_id or not key_path:
            raise SyncError('Enable Banking is not configured: set ENABLE_BANKING_APP_ID and ENABLE_BANKING_KEY_PATH in .env.')
        try:
            with open(key_path, 'rb') as handle:
                self._private_key = handle.read()
        except OSError:
            raise SyncError(f'Enable Banking private key not found at {key_path}.')
        self.app_id = app_id

    def _token(self):
        try:
            import jwt
        except ImportError:
            raise SyncError('PyJWT is not installed. Run: pip install -r requirements.txt')
        now = int(time.time())
        return jwt.encode(
            {'iss': 'enablebanking.com', 'aud': 'api.enablebanking.com', 'iat': now, 'exp': now + 3600},
            self._private_key,
            algorithm='RS256',
            headers={'kid': self.app_id},
        )

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(
                method,
                f'{ENABLE_BANKING_API}{path}',
                headers={'Authorization': f'Bearer {self._token()}'},
                timeout=HTTP_TIMEOUT,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise SyncError(f'Enable Banking request failed: {exc}')
        if response.status_code == 401 and path.startswith('/accounts'):
            raise SyncError('The bank consent expired or was revoked. Reconnect the bank.', 'reconnect')
        if response.status_code >= 400:
            try:
                detail = response.json().get('message') or response.text
            except ValueError:
                detail = response.text
            raise SyncError(f'Enable Banking error {response.status_code}: {detail[:300]}')
        return response.json()

    def find_aspsp(self, name, country):
        aspsps = self._request('GET', '/aspsps', params={'country': country}).get('aspsps', [])
        wanted = name.lower()
        for aspsp in aspsps:
            if aspsp.get('name', '').lower() == wanted:
                return aspsp
        for aspsp in aspsps:
            if wanted in aspsp.get('name', '').lower():
                return aspsp
        names = ', '.join(sorted(a.get('name', '') for a in aspsps))
        raise SyncError(f'{name} is not available via Enable Banking in {country}. Available: {names[:500]}')

    def start_authorization(self, aspsp, redirect_url, state):
        """Returns the bank's login URL the user must be sent to."""
        max_seconds = aspsp.get('maximum_consent_validity') or ENABLE_BANKING_MAX_CONSENT_DAYS * 86400
        days = min(ENABLE_BANKING_MAX_CONSENT_DAYS, int(max_seconds // 86400)) or 1
        valid_until = datetime.now(timezone.utc) + timedelta(days=days)
        payload = {
            'access': {'valid_until': valid_until.isoformat()},
            'aspsp': {'name': aspsp['name'], 'country': aspsp['country']},
            'state': state,
            'redirect_url': redirect_url,
            'psu_type': 'personal',
        }
        return self._request('POST', '/auth', json=payload)['url']

    def create_session(self, code):
        session = self._request('POST', '/sessions', json={'code': code})
        accounts = []
        for account in session.get('accounts', []):
            iban = ((account.get('account_id') or {}).get('iban') or '').replace(' ', '')
            accounts.append({
                'uid': account.get('uid'),
                'iban': iban,
                'name': account.get('name') or account.get('product') or '',
                'currency': account.get('currency') or 'EUR',
            })
        return {
            'session_id': session.get('session_id'),
            'valid_until': (session.get('access') or {}).get('valid_until'),
            'accounts': accounts,
        }

    def fetch_balance(self, account_uid):
        balances = self._request('GET', f'/accounts/{account_uid}/balances').get('balances', [])
        # Prefer booked/closing balance types over "available" ones.
        for preferred in ('CLBD', 'ITBD', 'XPCD', 'ITAV', 'CLAV'):
            for balance in balances:
                if balance.get('balance_type') == preferred:
                    return balance.get('balance_amount') or {}
        return (balances[0].get('balance_amount') or {}) if balances else {}

    def fetch_transactions(self, account_uid, date_from):
        transactions = []
        params = {'date_from': date_from.isoformat()}
        while True:
            page = self._request('GET', f'/accounts/{account_uid}/transactions', params=params)
            transactions.extend(page.get('transactions', []))
            continuation_key = page.get('continuation_key')
            if not continuation_key:
                return transactions
            params = {'date_from': date_from.isoformat(), 'continuation_key': continuation_key}


def open_banking_transactions_to_rows(account, transactions):
    """Normalize Enable Banking transactions of one account to CSV rows."""
    rows = []
    for t in transactions:
        status = (t.get('status') or '').upper()
        if status == 'PDNG':
            continue
        amount_info = t.get('transaction_amount') or {}
        amount = (amount_info.get('amount') or '0').strip()
        if (t.get('credit_debit_indicator') or '').upper() == 'DBIT' and not amount.startswith('-'):
            amount = f'-{amount}'
        creditor = (t.get('creditor') or {}).get('name') or ''
        debtor = (t.get('debtor') or {}).get('name') or ''
        is_debit = amount.startswith('-')
        remittance = t.get('remittance_information') or []
        if isinstance(remittance, str):
            remittance = [remittance]
        rows.append({
            'account_key': account['iban'] or account['uid'],
            'account_uid': account['uid'],
            'id': t.get('entry_reference') or t.get('transaction_id') or '',
            'booking_date': t.get('booking_date') or t.get('value_date') or t.get('transaction_date') or '',
            'value_date': t.get('value_date') or '',
            'amount': amount,
            'currency': amount_info.get('currency') or account['currency'],
            'description': ' | '.join(str(part).strip() for part in remittance if str(part).strip()),
            'payee': creditor if is_debit else debtor,
            'status': status,
        })
    return rows


def new_oauth_state():
    return uuid.uuid4().hex
