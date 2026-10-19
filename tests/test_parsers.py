"""Bank-export parsers and the shared amount/date/delimiter helpers.

All inputs are synthetic: never point these tests at files under data/raw.
"""
from datetime import datetime
from decimal import Decimal

import pytest

import app as ft
from tests.helpers import csv_text, write_xlsx


# --- shared helpers ---------------------------------------------------------

@pytest.mark.parametrize('raw, expected', [
    ('1.234,56', 1234.56),
    ('1,234.56', 1234.56),
    ('12,50', 12.5),
    ('-3.5', -3.5),
    ('12,5', 12.5),
    ('0,5', 0.5),
    ('1.234', 1234.0),
    ('1,234', 1234.0),
    ('1.234.567', 1234567.0),
    ('0.125', 0.125),
    ('1234.567', 1234.567),
    ('"  42 "', 42.0),
])
def test_parse_amount_string(raw, expected):
    assert ft.parse_amount_string(raw) == pytest.approx(expected)


def test_parse_amount_string_rejects_empty():
    with pytest.raises(ValueError):
        ft.parse_amount_string('')


@pytest.mark.parametrize('raw, expected', [
    ('2026-01-31', '2026-01-31'),
    ('31/01/2026', '2026-01-31'),
    ('01/31/2026', '2026-01-31'),
    ('31-01-2026', '2026-01-31'),
])
def test_parse_import_date(raw, expected):
    assert ft.parse_import_date(raw) == expected


def test_detect_delimiter_uses_header_line_only():
    # A ';' inside a description must not flip a comma file to ';'.
    sample = 'DATE,AMOUNT,DESCRIPTION\n2026-01-01,1.00,"rent; january"\n'
    assert ft.detect_delimiter(sample) == ','
    assert ft.detect_delimiter('DATE;AMOUNT\n2026-01-01;1,00\n') == ';'
    assert ft.detect_delimiter('DATE\tAMOUNT\n') == '\t'


# --- Revolut ------------------------------------------------------------------

REVOLUT_HEADERS = ['id', 'legId', 'type', 'state', 'startedDate', 'completedDate', 'createdDate',
                   'description', 'amount', 'amountWithCharges', 'currency', 'category',
                   'merchant_name', 'comment', 'reason']


def test_parse_revolut_csv():
    text = csv_text(REVOLUT_HEADERS, [
        ['r1', 'l1', 'CARD_PAYMENT', 'COMPLETED', '', '2026-03-02T10:00:00', '', 'Coffee', '-350', '-350',
         'EUR', 'restaurants', 'Cafe', '', ''],
        ['r2', 'l2a', 'EXCHANGE', 'COMPLETED', '', '2026-03-03T10:00:00', '', 'To USD', '-1000', '', 'EUR', '', '', '', ''],
        ['r2', 'l2b', 'EXCHANGE', 'COMPLETED', '', '2026-03-03T10:00:00', '', 'From EUR', '1080', '', 'USD', '', '', '', ''],
        ['r3', 'l3', 'TOPUP', 'PENDING', '', '', '2026-03-04T10:00:00', 'Top up', '5000', '', 'EUR', '', '', '', ''],
        ['r4', 'l4', 'CARD_PAYMENT', 'COMPLETED', '', '2026-03-05T10:00:00', '', 'Shop', '-1234', '-1250',
         'EUR', 'shopping', 'Shop', 'gift', ''],
    ])
    result = ft.parse_revolut_csv(text)
    rows = {row['id_transaction']: row for row in result['rows']}

    assert result['skipped'] == {'PENDING': 1}
    assert rows['rev_l1']['amount'] == Decimal('-3.50')
    assert rows['rev_l1']['transaction_type'] == 'expense'
    assert rows['rev_l1']['legacy_id'] == 'r1'
    assert rows['rev_l2a']['transaction_type'] == 'transfer'
    assert rows['rev_l2b']['currency'] == 'USD'
    # amountWithCharges wins over amount, comment is appended to the memo.
    assert rows['rev_l4']['amount'] == Decimal('-12.50')
    assert rows['rev_l4']['description'] == 'Shop | gift'


# --- Trade Republic -------------------------------------------------------------

TR_HEADERS = ['id_transaction', 'date', 'type', 'amount', 'currency', 'category', 'description',
              'counterparty_name', 'name', 'symbol', 'shares', 'price', 'asset_class']


def test_parse_trade_republic_csv():
    text = csv_text(TR_HEADERS, [
        ['t1', '2026-02-01', 'TRANSFER_INBOUND', '500.00', 'EUR', '', 'Deposit', 'Me', '', '', '', '', ''],
        ['t2', '2026-02-02', 'BUY', '-100.00', 'EUR', '', 'Buy ETF', '', 'World ETF', 'ie00b4l5y983', '1.5',
         '66.67', 'FUND'],
        ['t3', '2026-02-03', 'CARD_TRANSACTION', '-9.99', 'EUR', '', 'Card', 'Store', '', '', '', '', ''],
        ['', '2026-02-04', 'BUY', '-1', 'EUR', '', '', '', '', '', '', '', ''],
    ])
    result = ft.parse_trade_republic_csv(text)
    types = {row['id_transaction']: row['transaction_type'] for row in result['rows']}

    assert types == {'tr_t1': 'income', 'tr_t2': 'transfer', 'tr_t3': 'expense'}
    assert result['skipped'] == {'NO_ID_OR_DATE': 1}
    [buy] = result['investment_rows']
    assert buy['symbol'] == 'IE00B4L5Y983'
    assert buy['amount'] == Decimal('100.00')


# --- B100 -----------------------------------------------------------------------

B100_HEADERS = ['Fecha de Operación', 'Fecha valor', 'Detalle', 'Concepto', 'Cantidad',
                'Saldo tras operación', 'Divisa', 'Tipo de Movimiento']


def test_parse_b100_csv():
    text = csv_text(B100_HEADERS, [
        ['05/01/2026', '05/01/2026', 'Transferencia recibida', 'Nomina', '1500.00', '1500.00', 'EUR', ''],
        ['06/01/2026', '06/01/2026', 'Move to save', 'Hucha', '-100.00', '1400.00', 'EUR', ''],
        ['07/01/2026', '07/01/2026', 'Pago', 'Super', '-20.00', '1380.00', 'EUR', ''],
        ['07/01/2026', '07/01/2026', 'Pago', 'Super', '-20.00', '1380.00', 'EUR', ''],
    ])
    result = ft.parse_b100_csv(text)
    rows = result['rows']

    assert [row['transaction_type'] for row in rows] == ['income', 'transfer', 'expense', 'expense']
    assert rows[0]['date'] == '2026-01-05'
    # Identical rows stay distinct thanks to the occurrence index.
    assert rows[2]['id_transaction'] != rows[3]['id_transaction']


# --- Edenred --------------------------------------------------------------------

def test_parse_edenred_format_a(tmp_path):
    path = write_xlsx(tmp_path / 'Edenred Movimientos Cuenta Restaurante 20260101.xlsx', [
        ['Movimientos'],
        ['Fecha', 'Detalle', 'Importe'],
        [datetime(2026, 1, 2, 9, 0), 'RECARGA', 100],
        [datetime(2026, 1, 3, 13, 0), 'Bar Pepe - Madrid', 12.5],
        [datetime(2026, 1, 4, 13, 0), 'Bar Pepe - Devolucion', -2.5],
    ])
    result = ft.parse_edenred_xlsx(path, 'restaurant')
    rows = result['rows']

    assert [(row['transaction_type'], row['amount']) for row in rows] == [
        ('income', Decimal('100.00')),
        ('expense', Decimal('-12.50')),
        # A refund (negative charge) must come back as money in, not more spending.
        ('income', Decimal('2.50')),
    ]
    assert rows[1]['payee'] == 'Bar Pepe'
    assert ft.parse_edenred_xlsx(path, 'movilidad')['rows'] == []


def test_parse_edenred_format_b(tmp_path):
    path = write_xlsx(tmp_path / 'Edenred Movimientos Cuenta Movilidad 20250101.xlsx', [
        ['Tipo de Operación', 'Monto', 'Fecha'],
        ['Recarga', 40, datetime(2025, 1, 2)],
        ['Compra', -7.3, datetime(2025, 1, 3)],
    ])
    rows = ft.parse_edenred_xlsx(path, 'movilidad')['rows']
    assert [(row['transaction_type'], row['amount']) for row in rows] == [
        ('income', Decimal('40.00')),
        ('expense', Decimal('-7.30')),
    ]


# --- Iberia Cards ---------------------------------------------------------------

def test_parse_iberia_cards_xlsx(tmp_path):
    path = write_xlsx(tmp_path / 'extracto01012026.xlsx', [
        ['Extracto'],
        ['N°', None, 'FECHA OPERACIÓN', 'COMERCIO', 'IMPORTE DIVISA', 'IMPORTE EUROS'],
        [1, None, datetime(2025, 12, 1), 'AMAZON', None, 30.0],
        [2, None, '02/12/2025', 'REFUND SHOP', None, -5.0],
        [None, None, None, None, None, None],
        [None, None, None, 'TOTAL A CARGAR', None, 25.0],
        [3, None, datetime(2025, 12, 9), 'NOT IN A TABLE', None, 1.0],
    ])
    rows = ft.parse_iberia_cards_xlsx(path)['rows']
    assert [(row['date'], row['transaction_type'], row['amount']) for row in rows] == [
        ('2025-12-01', 'expense', Decimal('-30.00')),
        ('2025-12-02', 'income', Decimal('5.00')),
    ]


# --- Mintos ---------------------------------------------------------------------

def test_parse_mintos_xlsx(tmp_path):
    path = write_xlsx(tmp_path / 'Mintos 01_01_2026.xlsx', [
        ['Date', 'Transaction Type', 'Amount', 'Currency', 'Date from'],
        [datetime(2025, 6, 1), 'Deposits', 1000, 'EUR', datetime(2025, 1, 1)],
        [datetime(2025, 6, 2), 'Investment', -900, 'EUR', None],
        [datetime(2025, 7, 1), 'Interest received', 12.34, 'EUR', None],
        [datetime(2025, 7, 1), 'Tax withholding', -1.2, 'EUR', None],
        [datetime(2025, 8, 1), 'Something new', 5, 'EUR', None],
    ])
    result = ft.parse_mintos_xlsx(path)

    assert result['total_deposits'] == Decimal('1000.00')
    assert result['net_earnings'] == Decimal('11.14')
    assert result['current_total_value'] == Decimal('1011.14')
    assert result['outstanding_principal'] == Decimal('900.00')
    # The statement end is the latest row of any type, including unknown ones.
    assert result['statement_end_date'] == '2025-08-01'
    assert result['date_from'] == '2025-01-01'
    assert result['skipped'] == {'UNKNOWN_TYPE:Something new': 1}
