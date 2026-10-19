use crate::AppState;
use rusqlite::{params, params_from_iter, types::Value};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::collections::HashSet;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Transaction {
    pub id: i64,
    pub id_transaction: Option<String>,
    pub account_id: i64,
    pub account_name: Option<String>,
    pub account_currency: Option<String>,
    pub category_id: Option<i64>,
    pub category_name: Option<String>,
    pub category_color: Option<String>,
    pub category_icon: Option<String>,
    pub transaction_type: String,
    pub amount: f64,
    pub description: Option<String>,
    pub payee: Option<String>,
    pub date: String,
    pub created_at: String,
}

#[derive(Debug, Deserialize)]
pub struct TransactionFilter {
    pub account_id: Option<i64>,
    pub category_id: Option<i64>,
    pub transaction_type: Option<String>,
    pub search: Option<String>,
    pub date_from: Option<String>,
    pub date_to: Option<String>,
    pub limit: Option<i64>,
    pub offset: Option<i64>,
}

#[derive(Debug, Serialize)]
pub struct ImportResult {
    pub imported: usize,
    pub updated: usize,
    pub skipped: usize,
    pub errors: Vec<String>,
}

fn fallback_transaction_id(
    account_name: &str,
    account_currency: &str,
    date: &str,
    amount: f64,
    description: &str,
    payee: &str,
) -> String {
    format!(
        "auto|{}|{}|{}|{:.2}|{}|{}",
        account_name.trim().to_lowercase(),
        account_currency.trim().to_uppercase(),
        date.trim(),
        amount.abs(),
        description.trim().to_lowercase(),
        payee.trim().to_lowercase(),
    )
}

#[tauri::command]
pub fn get_transactions(
    state: tauri::State<AppState>,
    filter: Option<TransactionFilter>,
) -> Result<Vec<Transaction>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut sql = String::from(
        "SELECT t.id, t.id_transaction, t.account_id, a.name, a.currency, t.category_id, c.name, c.color, c.icon,
                t.transaction_type, t.amount, t.description, t.payee, t.date, t.created_at
         FROM transactions t
         LEFT JOIN accounts a ON t.account_id = a.id
         LEFT JOIN categories c ON t.category_id = c.id
         WHERE 1=1",
    );

    // Every filter value is bound as a parameter, never spliced into the SQL.
    let mut values: Vec<Value> = Vec::new();

    if let Some(ref f) = filter {
        if let Some(aid) = f.account_id {
            sql.push_str(" AND t.account_id = ?");
            values.push(Value::Integer(aid));
        }
        if let Some(cid) = f.category_id {
            sql.push_str(" AND t.category_id = ?");
            values.push(Value::Integer(cid));
        }
        if let Some(ref tt) = f.transaction_type {
            sql.push_str(" AND t.transaction_type = ?");
            values.push(Value::Text(tt.clone()));
        }
        if let Some(ref s) = f.search {
            // Escape LIKE wildcards so '%' and '_' in the search box match literally.
            let escaped = s.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_");
            let pattern = format!("%{}%", escaped);
            sql.push_str(" AND (t.description LIKE ? ESCAPE '\\' OR t.payee LIKE ? ESCAPE '\\')");
            values.push(Value::Text(pattern.clone()));
            values.push(Value::Text(pattern));
        }
        if let Some(ref df) = f.date_from {
            sql.push_str(" AND t.date >= ?");
            values.push(Value::Text(df.clone()));
        }
        if let Some(ref dt) = f.date_to {
            sql.push_str(" AND t.date <= ?");
            values.push(Value::Text(dt.clone()));
        }
    }

    sql.push_str(" ORDER BY t.date DESC, t.id DESC");

    if let Some(ref f) = filter {
        if f.limit.is_some() || f.offset.is_some() {
            // SQLite only accepts OFFSET after a LIMIT; -1 means "no limit".
            sql.push_str(" LIMIT ? OFFSET ?");
            values.push(Value::Integer(f.limit.unwrap_or(-1)));
            values.push(Value::Integer(f.offset.unwrap_or(0)));
        }
    }

    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;

    let transactions = stmt
        .query_map(params_from_iter(values.iter()), |row| {
            Ok(Transaction {
                id: row.get(0)?,
                id_transaction: row.get(1)?,
                account_id: row.get(2)?,
                account_name: row.get(3)?,
                account_currency: row.get(4)?,
                category_id: row.get(5)?,
                category_name: row.get(6)?,
                category_color: row.get(7)?,
                category_icon: row.get(8)?,
                transaction_type: row.get(9)?,
                amount: row.get(10)?,
                description: row.get(11)?,
                payee: row.get(12)?,
                date: row.get(13)?,
                created_at: row.get(14)?,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    Ok(transactions)
}

#[tauri::command]
pub fn add_transaction(
    state: tauri::State<AppState>,
    account_id: i64,
    category_id: Option<i64>,
    transaction_type: String,
    amount: f64,
    description: Option<String>,
    payee: Option<String>,
    date: String,
) -> Result<Transaction, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;
    // Row + balance change commit together (an early `?` return rolls back).
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;

    tx.execute(
        "INSERT INTO transactions (account_id, category_id, transaction_type, amount, description, payee, date)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![account_id, category_id, transaction_type, amount, description, payee, date],
    )
    .map_err(|e| e.to_string())?;
    let id = tx.last_insert_rowid();

    // Update account balance
    let delta = match transaction_type.as_str() {
        "income" => amount,
        "expense" => -amount,
        _ => 0.0,
    };
    if delta != 0.0 {
        tx.execute(
            "UPDATE accounts SET balance = balance + ?1 WHERE id = ?2",
            params![delta, account_id],
        )
        .map_err(|e| e.to_string())?;
    }

    tx.commit().map_err(|e| e.to_string())?;
    fetch_transaction(conn, id)
}

#[tauri::command]
pub fn update_transaction(
    state: tauri::State<AppState>,
    id: i64,
    account_id: i64,
    category_id: Option<i64>,
    transaction_type: String,
    amount: f64,
    description: Option<String>,
    payee: Option<String>,
    date: String,
) -> Result<Transaction, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;

    // Reverse old balance effect
    let (old_account_id, old_type, old_amount): (i64, String, f64) = tx
        .query_row(
            "SELECT account_id, transaction_type, amount FROM transactions WHERE id = ?1",
            params![id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .map_err(|e| e.to_string())?;

    let old_delta = match old_type.as_str() {
        "income" => -old_amount,
        "expense" => old_amount,
        _ => 0.0,
    };
    if old_delta != 0.0 {
        tx.execute(
            "UPDATE accounts SET balance = balance + ?1 WHERE id = ?2",
            params![old_delta, old_account_id],
        )
        .map_err(|e| e.to_string())?;
    }

    tx.execute(
        "UPDATE transactions SET account_id=?1, category_id=?2, transaction_type=?3,
         amount=?4, description=?5, payee=?6, date=?7 WHERE id=?8",
        params![account_id, category_id, transaction_type, amount, description, payee, date, id],
    )
    .map_err(|e| e.to_string())?;

    // Apply new balance effect
    let new_delta = match transaction_type.as_str() {
        "income" => amount,
        "expense" => -amount,
        _ => 0.0,
    };
    if new_delta != 0.0 {
        tx.execute(
            "UPDATE accounts SET balance = balance + ?1 WHERE id = ?2",
            params![new_delta, account_id],
        )
        .map_err(|e| e.to_string())?;
    }

    tx.commit().map_err(|e| e.to_string())?;
    fetch_transaction(conn, id)
}

#[tauri::command]
pub fn delete_transaction(state: tauri::State<AppState>, id: i64) -> Result<(), String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;

    let (account_id, transaction_type, amount): (i64, String, f64) = tx
        .query_row(
            "SELECT account_id, transaction_type, amount FROM transactions WHERE id = ?1",
            params![id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .map_err(|e| e.to_string())?;

    tx.execute("DELETE FROM transactions WHERE id=?1", params![id])
        .map_err(|e| e.to_string())?;

    let delta = match transaction_type.as_str() {
        "income" => -amount,
        "expense" => amount,
        _ => 0.0,
    };
    if delta != 0.0 {
        tx.execute(
            "UPDATE accounts SET balance = balance + ?1 WHERE id = ?2",
            params![delta, account_id],
        )
        .map_err(|e| e.to_string())?;
    }

    tx.commit().map_err(|e| e.to_string())
}

#[tauri::command]
pub fn import_csv(
    state: tauri::State<AppState>,
    csv_content: String,
    account_id: i64,
    column_map: HashMap<String, usize>,
    transaction_type: String,
    date_format: String,
    has_header: bool,
) -> Result<ImportResult, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;
    // One transaction for the whole file: faster, and all-or-nothing on a hard error.
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;

    let mut rdr = csv::ReaderBuilder::new()
        .has_headers(has_header)
        .flexible(true)
        .from_reader(csv_content.as_bytes());

    let mut imported = 0;
    let mut skipped = 0;
    let mut errors: Vec<String> = Vec::new();

    for (i, result) in rdr.records().enumerate() {
        let record = match result {
            Ok(r) => r,
            Err(e) => {
                errors.push(format!("Row {}: parse error: {}", i + 1, e));
                skipped += 1;
                continue;
            }
        };

        let get_col = |key: &str| -> Option<String> {
            column_map
                .get(key)
                .and_then(|&idx| record.get(idx))
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
        };

        let date_raw = match get_col("date") {
            Some(d) => d,
            None => {
                skipped += 1;
                continue;
            }
        };

        // Normalize date to YYYY-MM-DD
        let date = normalize_date(&date_raw, &date_format).unwrap_or(date_raw);

        let amount_raw = match get_col("amount") {
            Some(a) => a,
            None => {
                skipped += 1;
                continue;
            }
        };

        let amount: f64 = match amount_raw
            .replace(',', ".")
            .replace([' ', '\u{a0}'], "")
            .trim_start_matches('+')
            .parse()
        {
            Ok(v) => v,
            Err(_) => {
                errors.push(format!("Row {}: invalid amount '{}'", i + 1, amount_raw));
                skipped += 1;
                continue;
            }
        };

        let (actual_amount, actual_type) = if amount < 0.0 {
            (-amount, "expense")
        } else {
            (amount, "income")
        };

        let final_type = if transaction_type != "auto" {
            transaction_type.as_str()
        } else {
            actual_type
        };

        let description = get_col("description");
        let payee = get_col("payee");

        if let Err(e) = tx.execute(
            "INSERT INTO transactions (account_id, transaction_type, amount, description, payee, date)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![account_id, final_type, actual_amount, description, payee, date],
        ) {
            // A failed row must not touch the balance or count as imported.
            errors.push(format!("Row {}: insert failed: {}", i + 1, e));
            skipped += 1;
            continue;
        }

        let balance_delta = match final_type {
            "income" => actual_amount,
            "expense" => -actual_amount,
            _ => 0.0,
        };
        tx.execute(
            "UPDATE accounts SET balance = balance + ?1 WHERE id = ?2",
            params![balance_delta, account_id],
        )
        .map_err(|e| e.to_string())?;

        imported += 1;
    }

    tx.commit().map_err(|e| e.to_string())?;

    Ok(ImportResult {
        imported,
        updated: 0,
        skipped,
        errors,
    })
}

#[tauri::command]
pub fn export_transactions_csv(
    state: tauri::State<AppState>,
    filter: Option<TransactionFilter>,
) -> Result<String, String> {
    let transactions = get_transactions(state, filter)?;
    let mut writer = csv::Writer::from_writer(Vec::new());
    writer
        .write_record([
            "ID_TRANSACTION",
            "DATE",
            "AMOUNT",
            "CATEGORY",
            "DESCRIPTION",
            "PAYEE",
            "ACCOUNT_NAME",
            "ACCOUNT_CURRENCY",
            "TRANSACTION_TYPE",
        ])
        .map_err(|e| e.to_string())?;

    for tx in transactions {
        let sign_amount = if tx.transaction_type == "expense" {
            -tx.amount
        } else {
            tx.amount
        };
        writer
            .write_record([
                tx.id_transaction.unwrap_or_default(),
                tx.date,
                format!("{:.2}", sign_amount),
                tx.category_name.unwrap_or_default(),
                tx.description.unwrap_or_default(),
                tx.payee.unwrap_or_default(),
                tx.account_name.unwrap_or_default(),
                tx.account_currency.unwrap_or_else(|| "USD".to_string()),
                tx.transaction_type,
            ])
            .map_err(|e| e.to_string())?;
    }

    writer.flush().map_err(|e| e.to_string())?;
    let bytes = writer.into_inner().map_err(|e| e.to_string())?;
    String::from_utf8(bytes).map_err(|e| e.to_string())
}

#[tauri::command]
pub fn import_transactions_canonical_csv(
    state: tauri::State<AppState>,
    csv_content: String,
) -> Result<ImportResult, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut category_map: HashMap<String, i64> = HashMap::new();
    let mut cat_stmt = conn
        .prepare("SELECT id, name FROM categories")
        .map_err(|e| e.to_string())?;
    let cat_rows = cat_stmt
        .query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)))
        .map_err(|e| e.to_string())?;
    for row in cat_rows {
        let (id, name) = row.map_err(|e| e.to_string())?;
        category_map.insert(name.trim().to_lowercase(), id);
    }

    let mut account_map: HashMap<String, i64> = HashMap::new();
    let mut acc_stmt = conn
        .prepare("SELECT id, name, currency FROM accounts")
        .map_err(|e| e.to_string())?;
    let acc_rows = acc_stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?;
    for row in acc_rows {
        let (id, name, currency) = row.map_err(|e| e.to_string())?;
        account_map.insert(
            format!("{}||{}", name.trim().to_lowercase(), currency.trim().to_uppercase()),
            id,
        );
    }

    let mut reader = csv::ReaderBuilder::new()
        .flexible(true)
        .from_reader(csv_content.as_bytes());
    let headers = reader.headers().map_err(|e| e.to_string())?.clone();

    let idx = |name: &str| {
        headers
            .iter()
            .position(|h| h.trim().eq_ignore_ascii_case(name))
    };

    let id_idx = idx("ID_TRANSACTION");
    let date_idx = idx("DATE").ok_or("Missing DATE column")?;
    let amount_idx = idx("AMOUNT").ok_or("Missing AMOUNT column")?;
    let category_idx = idx("CATEGORY");
    let description_idx = idx("DESCRIPTION");
    let payee_idx = idx("PAYEE");
    let account_name_idx = idx("ACCOUNT_NAME").ok_or("Missing ACCOUNT_NAME column")?;
    let account_currency_idx = idx("ACCOUNT_CURRENCY");
    let type_idx = idx("TRANSACTION_TYPE");

    let mut imported = 0;
    let mut updated = 0;
    let mut skipped = 0;
    let mut errors: Vec<String> = Vec::new();
    let mut touched_accounts: HashSet<i64> = HashSet::new();

    for (row_num, result) in reader.records().enumerate() {
        let record = match result {
            Ok(r) => r,
            Err(e) => {
                skipped += 1;
                errors.push(format!("Row {}: {}", row_num + 2, e));
                continue;
            }
        };

        let account_name = record.get(account_name_idx).unwrap_or("").trim().to_string();
        if account_name.is_empty() {
            skipped += 1;
            continue;
        }
        let account_currency = account_currency_idx
            .and_then(|i| record.get(i))
            .unwrap_or("USD")
            .trim()
            .to_uppercase();
        let account_key = format!("{}||{}", account_name.to_lowercase(), account_currency);
        let account_id = if let Some(id) = account_map.get(&account_key).copied() {
            id
        } else {
            conn.execute(
                "INSERT INTO accounts (name, account_type, balance, currency) VALUES (?1, 'checking', 0, ?2)",
                params![account_name, account_currency],
            )
            .map_err(|e| e.to_string())?;
            let id = conn.last_insert_rowid();
            account_map.insert(account_key, id);
            id
        };

        let date = record.get(date_idx).unwrap_or("").trim().to_string();
        let parsed_amount = record
            .get(amount_idx)
            .unwrap_or("0")
            .trim()
            .replace(',', ".")
            .parse::<f64>();

        let amount = match parsed_amount {
            Ok(v) => v,
            Err(_) => {
                skipped += 1;
                errors.push(format!("Row {}: invalid amount", row_num + 2));
                continue;
            }
        };

        let mut transaction_type = type_idx
            .and_then(|i| record.get(i))
            .unwrap_or("")
            .trim()
            .to_lowercase();
        if !["income", "expense", "transfer"].contains(&transaction_type.as_str()) {
            transaction_type = if amount < 0.0 {
                "expense".to_string()
            } else {
                "income".to_string()
            };
        }

        let normalized_amount = amount.abs();
        let description = description_idx
            .and_then(|i| record.get(i))
            .unwrap_or("")
            .trim()
            .to_string();
        let payee = payee_idx
            .and_then(|i| record.get(i))
            .unwrap_or("")
            .trim()
            .to_string();

        let category_id = category_idx
            .and_then(|i| record.get(i))
            .map(|v| v.trim().to_lowercase())
            .and_then(|v| category_map.get(&v).copied());

        let id_transaction = id_idx
            .and_then(|i| record.get(i))
            .unwrap_or("")
            .trim()
            .to_string();
        let stable_id = if id_transaction.is_empty() {
            fallback_transaction_id(
                &account_name,
                &account_currency,
                &date,
                normalized_amount,
                &description,
                &payee,
            )
        } else {
            id_transaction
        };

        let existing = conn
            .query_row(
                "SELECT id FROM transactions WHERE account_id = ?1 AND id_transaction = ?2 ORDER BY id DESC LIMIT 1",
                params![account_id, stable_id],
                |row| row.get::<_, i64>(0),
            )
            .ok();

        if let Some(tx_id) = existing {
            conn.execute(
                "UPDATE transactions SET category_id = ?1, transaction_type = ?2, amount = ?3, description = ?4, payee = ?5, date = ?6 WHERE id = ?7",
                params![category_id, transaction_type, normalized_amount, description, payee, date, tx_id],
            )
            .map_err(|e| e.to_string())?;
            updated += 1;
            touched_accounts.insert(account_id);
        } else {
            conn.execute(
                "INSERT INTO transactions (account_id, category_id, transaction_type, amount, description, payee, date, id_transaction) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![account_id, category_id, transaction_type, normalized_amount, description, payee, date, stable_id],
            )
            .map_err(|e| e.to_string())?;
            imported += 1;
            touched_accounts.insert(account_id);
        }
    }

    for account_id in touched_accounts {
        let recalculated_balance: f64 = conn
            .query_row(
                "SELECT COALESCE(SUM(CASE WHEN transaction_type='income' THEN amount WHEN transaction_type='expense' THEN -amount ELSE 0 END), 0) FROM transactions WHERE account_id = ?1",
                params![account_id],
                |row| row.get(0),
            )
            .map_err(|e| e.to_string())?;

        conn.execute(
            "UPDATE accounts SET balance = ?1 WHERE id = ?2",
            params![recalculated_balance, account_id],
        )
        .map_err(|e| e.to_string())?;
    }

    Ok(ImportResult {
        imported,
        updated,
        skipped,
        errors,
    })
}

fn normalize_date(raw: &str, format: &str) -> Option<String> {
    let parts: Vec<&str> = raw.split(['-', '/', '.']).collect();
    if parts.len() != 3 {
        return None;
    }
    match format {
        "YYYY-MM-DD" => Some(raw.replace(['/', '.'], "-")),
        "DD/MM/YYYY" => Some(format!("{}-{}-{}", parts[2], parts[1], parts[0])),
        "MM/DD/YYYY" => Some(format!("{}-{}-{}", parts[2], parts[0], parts[1])),
        _ => Some(raw.to_string()),
    }
}

fn fetch_transaction(conn: &rusqlite::Connection, id: i64) -> Result<Transaction, String> {
    conn.query_row(
    "SELECT t.id, t.id_transaction, t.account_id, a.name, a.currency, t.category_id, c.name, c.color, c.icon,
                t.transaction_type, t.amount, t.description, t.payee, t.date, t.created_at
         FROM transactions t
         LEFT JOIN accounts a ON t.account_id = a.id
         LEFT JOIN categories c ON t.category_id = c.id
         WHERE t.id = ?1",
        params![id],
        |row| {
            Ok(Transaction {
                id: row.get(0)?,
                id_transaction: row.get(1)?,
                account_id: row.get(2)?,
                account_name: row.get(3)?,
                account_currency: row.get(4)?,
                category_id: row.get(5)?,
                category_name: row.get(6)?,
                category_color: row.get(7)?,
                category_icon: row.get(8)?,
                transaction_type: row.get(9)?,
                amount: row.get(10)?,
                description: row.get(11)?,
                payee: row.get(12)?,
                date: row.get(13)?,
                created_at: row.get(14)?,
            })
        },
    )
    .map_err(|e| e.to_string())
}
