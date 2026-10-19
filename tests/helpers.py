"""Builders for synthetic import files and investment fixtures."""
import csv
import io

import openpyxl


def csv_text(headers, rows, delimiter=','):
    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter)
    writer.writerow(headers)
    writer.writerows(rows)
    return out.getvalue()


def write_xlsx(path, rows):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return str(path)


def make_account(db, user_id, name='Main', currency='EUR', account_type='checking'):
    cursor = db.execute(
        'INSERT INTO accounts (user_id, name, account_type, currency) VALUES (?, ?, ?, ?)',
        (user_id, name, account_type, currency),
    )
    db.commit()
    return cursor.lastrowid


def make_investment_account(db, user_id, name='Broker', currency='EUR'):
    cursor = db.execute(
        '''INSERT INTO accounts (user_id, name, account_type, currency, is_investment_dashboard)
           VALUES (?, ?, 'investment', ?, 1)''',
        (user_id, name, currency),
    )
    db.commit()
    return cursor.lastrowid


def make_holding(db, user_id, account_id, asset_class, quantity, avg_cost, price, currency='EUR', name='Asset'):
    cursor = db.execute(
        '''INSERT INTO investment_holdings
           (user_id, account_id, asset_name, ticker, isin, asset_class, currency, quantity, avg_cost, current_price)
           VALUES (?, ?, ?, '', '', ?, ?, ?, ?, ?)''',
        (user_id, account_id, name, asset_class, currency, quantity, avg_cost, price),
    )
    db.commit()
    return cursor.lastrowid


def add_operation(db, user_id, account_id, holding_id, operation_type, amount, date, note=None, currency='EUR'):
    db.execute(
        '''INSERT INTO investment_operations
           (user_id, account_id, holding_id, operation_type, amount, currency, note, operation_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (user_id, account_id, holding_id, operation_type, amount, currency, note, date),
    )
    db.commit()
