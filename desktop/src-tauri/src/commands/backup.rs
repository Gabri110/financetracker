use crate::AppState;
use base64::{engine::general_purpose, Engine as _};
use calamine::{open_workbook_auto_from_rs, Reader};
use csv::{ReaderBuilder, Writer};
use rusqlite::params;
use rust_xlsxwriter::Workbook;
use std::collections::{HashMap, HashSet};
use std::io::{Cursor, Read, Write};
use zip::{write::SimpleFileOptions, ZipArchive, ZipWriter};

#[derive(Debug, serde::Serialize)]
pub struct FullBackupImportResult {
    pub accounts_inserted: usize,
    pub accounts_updated: usize,
    pub categories_inserted: usize,
    pub categories_updated: usize,
    pub rules_inserted: usize,
    pub rules_updated: usize,
    pub transactions_imported: usize,
    pub transactions_updated: usize,
    pub skipped: usize,
    pub warnings: Vec<String>,
}

fn normalize_key(parts: &[&str]) -> String {
    parts
        .iter()
        .map(|p| p.trim().to_lowercase())
        .collect::<Vec<_>>()
        .join("||")
}

fn normalize_currency(value: &str) -> String {
    let code = value.trim().to_uppercase();
    if code.is_empty() {
        "USD".to_string()
    } else {
        code
    }
}

fn stable_transaction_id(
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

fn slugify(name: &str) -> String {
    let mut out = String::new();
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() {
            out.push(ch.to_ascii_lowercase());
        } else if !out.ends_with('_') {
            out.push('_');
        }
    }
    out.trim_matches('_').to_string()
}

fn export_accounts_csv(conn: &rusqlite::Connection) -> Result<String, String> {
    let mut writer = Writer::from_writer(Vec::new());
    writer
        .write_record([
            "ACCOUNT_NAME",
            "ACCOUNT_TYPE",
            "CURRENCY",
            "CURRENT_BALANCE",
            "CREATED_AT",
        ])
        .map_err(|e| e.to_string())?;

    let mut stmt = conn
        .prepare("SELECT name, account_type, currency, balance, created_at FROM accounts ORDER BY name")
        .map_err(|e| e.to_string())?;

    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, f64>(3)?,
                row.get::<_, String>(4)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    for row in rows {
        let (name, account_type, currency, balance, created_at) = row.map_err(|e| e.to_string())?;
        writer
            .write_record([
                name,
                account_type,
                normalize_currency(&currency),
                format!("{:.2}", balance),
                created_at,
            ])
            .map_err(|e| e.to_string())?;
    }

    writer.flush().map_err(|e| e.to_string())?;
    let bytes = writer.into_inner().map_err(|e| e.to_string())?;
    String::from_utf8(bytes).map_err(|e| e.to_string())
}

fn export_categories_csv(conn: &rusqlite::Connection) -> Result<String, String> {
    let mut writer = Writer::from_writer(Vec::new());
    writer
        .write_record(["NAME", "CATEGORY_TYPE", "ICON", "COLOR"])
        .map_err(|e| e.to_string())?;

    let mut stmt = conn
        .prepare("SELECT name, category_type, icon, color FROM categories ORDER BY category_type, name")
        .map_err(|e| e.to_string())?;

    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    for row in rows {
        let (name, category_type, icon, color) = row.map_err(|e| e.to_string())?;
        writer
            .write_record([name, category_type, icon, color])
            .map_err(|e| e.to_string())?;
    }

    writer.flush().map_err(|e| e.to_string())?;
    let bytes = writer.into_inner().map_err(|e| e.to_string())?;
    String::from_utf8(bytes).map_err(|e| e.to_string())
}

fn export_rules_csv(conn: &rusqlite::Connection) -> Result<String, String> {
    let mut writer = Writer::from_writer(Vec::new());
    writer
        .write_record([
            "PATTERN",
            "CATEGORY_NAME",
            "CATEGORY_TYPE",
            "MATCH_FIELD",
            "IS_REGEX",
            "PRIORITY",
        ])
        .map_err(|e| e.to_string())?;

    let mut stmt = conn
        .prepare(
            "SELECT r.pattern, c.name, c.category_type, r.match_field, r.is_regex, r.priority
             FROM category_rules r
             JOIN categories c ON r.category_id = c.id
             ORDER BY r.priority DESC, r.pattern",
        )
        .map_err(|e| e.to_string())?;

    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, i64>(5)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    for row in rows {
        let (pattern, category_name, category_type, match_field, is_regex, priority) =
            row.map_err(|e| e.to_string())?;
        writer
            .write_record([
                pattern,
                category_name,
                category_type,
                match_field,
                is_regex.to_string(),
                priority.to_string(),
            ])
            .map_err(|e| e.to_string())?;
    }

    writer.flush().map_err(|e| e.to_string())?;
    let bytes = writer.into_inner().map_err(|e| e.to_string())?;
    String::from_utf8(bytes).map_err(|e| e.to_string())
}

fn export_transactions_csv(conn: &rusqlite::Connection) -> Result<String, String> {
    let mut writer = Writer::from_writer(Vec::new());
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

    let mut stmt = conn
        .prepare(
            "SELECT t.id_transaction, t.date, t.amount, t.transaction_type, t.description, t.payee,
                    a.name, a.currency, c.name
             FROM transactions t
             JOIN accounts a ON t.account_id = a.id
             LEFT JOIN categories c ON t.category_id = c.id
             ORDER BY t.date DESC, t.id DESC",
        )
        .map_err(|e| e.to_string())?;

    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, Option<String>>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, f64>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, Option<String>>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, String>(6)?,
                row.get::<_, String>(7)?,
                row.get::<_, Option<String>>(8)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    for row in rows {
        let (
            id_transaction,
            date,
            amount,
            transaction_type,
            description,
            payee,
            account_name,
            account_currency,
            category,
        ) = row.map_err(|e| e.to_string())?;

        let signed_amount = if transaction_type == "expense" { -amount } else { amount };

        writer
            .write_record([
                id_transaction.unwrap_or_default(),
                date,
                format!("{:.2}", signed_amount),
                category.unwrap_or_default(),
                description.unwrap_or_default(),
                payee.unwrap_or_default(),
                account_name,
                normalize_currency(&account_currency),
                transaction_type,
            ])
            .map_err(|e| e.to_string())?;
    }

    writer.flush().map_err(|e| e.to_string())?;
    let bytes = writer.into_inner().map_err(|e| e.to_string())?;
    String::from_utf8(bytes).map_err(|e| e.to_string())
}

fn csv_to_rows(csv_content: &str) -> Result<Vec<Vec<String>>, String> {
    let mut reader = ReaderBuilder::new()
        .flexible(true)
        .from_reader(csv_content.as_bytes());
    let mut out = Vec::new();
    for result in reader.records() {
        let record = result.map_err(|e| e.to_string())?;
        out.push(record.iter().map(|s| s.to_string()).collect());
    }
    Ok(out)
}

fn write_sheet_from_csv(workbook: &mut Workbook, sheet_name: &str, csv_content: &str) -> Result<(), String> {
    let mut worksheet = workbook.add_worksheet();
    worksheet.set_name(sheet_name).map_err(|e| e.to_string())?;

    let mut reader = ReaderBuilder::new()
        .flexible(true)
        .from_reader(csv_content.as_bytes());

    let headers = reader.headers().map_err(|e| e.to_string())?.clone();
    for (col, value) in headers.iter().enumerate() {
        worksheet
            .write_string(0, col as u16, value)
            .map_err(|e| e.to_string())?;
    }

    for (row_idx, result) in reader.records().enumerate() {
        let record = result.map_err(|e| e.to_string())?;
        for (col, value) in record.iter().enumerate() {
            worksheet
                .write_string((row_idx + 1) as u32, col as u16, value)
                .map_err(|e| e.to_string())?;
        }
    }

    Ok(())
}

fn parse_dict_rows(csv_content: &str) -> Result<Vec<HashMap<String, String>>, String> {
    let mut reader = ReaderBuilder::new()
        .flexible(true)
        .from_reader(csv_content.as_bytes());

    let headers = reader
        .headers()
        .map_err(|e| e.to_string())?
        .iter()
        .map(|h| h.trim().to_uppercase())
        .collect::<Vec<_>>();

    let mut out = Vec::new();
    for result in reader.records() {
        let record = result.map_err(|e| e.to_string())?;
        let mut row = HashMap::new();
        let mut has_value = false;
        for (idx, header) in headers.iter().enumerate() {
            let value = record.get(idx).unwrap_or("").trim().to_string();
            if !value.is_empty() {
                has_value = true;
            }
            row.insert(header.clone(), value);
        }
        if has_value {
            out.push(row);
        }
    }
    Ok(out)
}

fn recalc_balance_for_account(conn: &rusqlite::Connection, account_id: i64) -> Result<(), String> {
    let balance: f64 = conn
        .query_row(
            "SELECT COALESCE(SUM(CASE WHEN transaction_type='income' THEN amount WHEN transaction_type='expense' THEN -amount ELSE 0 END), 0) FROM transactions WHERE account_id = ?1",
            params![account_id],
            |row| row.get(0),
        )
        .map_err(|e| e.to_string())?;

    conn.execute(
        "UPDATE accounts SET balance = ?1 WHERE id = ?2",
        params![balance, account_id],
    )
    .map_err(|e| e.to_string())?;

    Ok(())
}

fn import_accounts_rows(
    conn: &rusqlite::Connection,
    rows: &[HashMap<String, String>],
) -> Result<(HashMap<String, i64>, usize, usize), String> {
    let mut map: HashMap<String, i64> = HashMap::new();

    let mut stmt = conn
        .prepare("SELECT id, name, account_type, currency FROM accounts")
        .map_err(|e| e.to_string())?;
    let existing = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    for row in existing {
        let (id, name, account_type, currency) = row.map_err(|e| e.to_string())?;
        map.insert(normalize_key(&[&name, &normalize_currency(&currency), &account_type]), id);
    }

    let mut inserted = 0;
    let mut updated = 0;

    for row in rows {
        let name = row.get("ACCOUNT_NAME").cloned().unwrap_or_default();
        if name.trim().is_empty() {
            continue;
        }
        let account_type = row
            .get("ACCOUNT_TYPE")
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_else(|| "checking".to_string());
        let currency = normalize_currency(row.get("CURRENCY").map(String::as_str).unwrap_or("USD"));
        let balance = row
            .get("CURRENT_BALANCE")
            .and_then(|s| s.replace(',', ".").parse::<f64>().ok())
            .unwrap_or(0.0);

        let key = normalize_key(&[&name, &currency, &account_type]);
        if let Some(id) = map.get(&key).copied() {
            conn.execute(
                "UPDATE accounts SET balance = ?1 WHERE id = ?2",
                params![balance, id],
            )
            .map_err(|e| e.to_string())?;
            updated += 1;
        } else {
            conn.execute(
                "INSERT INTO accounts (name, account_type, balance, currency) VALUES (?1, ?2, ?3, ?4)",
                params![name, account_type, balance, currency],
            )
            .map_err(|e| e.to_string())?;
            let id = conn.last_insert_rowid();
            map.insert(key, id);
            inserted += 1;
        }
    }

    Ok((map, inserted, updated))
}

fn import_categories_rows(
    conn: &rusqlite::Connection,
    rows: &[HashMap<String, String>],
) -> Result<(HashMap<String, i64>, usize, usize), String> {
    let mut map: HashMap<String, i64> = HashMap::new();

    let mut stmt = conn
        .prepare("SELECT id, name, category_type FROM categories")
        .map_err(|e| e.to_string())?;
    let existing = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    for row in existing {
        let (id, name, category_type) = row.map_err(|e| e.to_string())?;
        map.insert(normalize_key(&[&name, &category_type]), id);
    }

    let mut inserted = 0;
    let mut updated = 0;

    for row in rows {
        let name = row.get("NAME").cloned().unwrap_or_default();
        let category_type = row
            .get("CATEGORY_TYPE")
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_default();
        if name.trim().is_empty() || !["income", "expense", "transfer"].contains(&category_type.as_str()) {
            continue;
        }

        let icon = row
            .get("ICON")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "tag".to_string());
        let color = row
            .get("COLOR")
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| "#6c757d".to_string());

        let key = normalize_key(&[&name, &category_type]);
        if let Some(id) = map.get(&key).copied() {
            conn.execute(
                "UPDATE categories SET icon = ?1, color = ?2 WHERE id = ?3",
                params![icon, color, id],
            )
            .map_err(|e| e.to_string())?;
            updated += 1;
        } else {
            conn.execute(
                "INSERT INTO categories (name, category_type, icon, color) VALUES (?1, ?2, ?3, ?4)",
                params![name, category_type, icon, color],
            )
            .map_err(|e| e.to_string())?;
            let id = conn.last_insert_rowid();
            map.insert(key, id);
            inserted += 1;
        }
    }

    Ok((map, inserted, updated))
}

fn import_rules_rows(
    conn: &rusqlite::Connection,
    rows: &[HashMap<String, String>],
    category_map: &HashMap<String, i64>,
) -> Result<(usize, usize, usize, Vec<String>), String> {
    let mut inserted = 0;
    let mut updated = 0;
    let mut skipped = 0;
    let mut warnings = Vec::new();

    for (idx, row) in rows.iter().enumerate() {
        let pattern = row.get("PATTERN").cloned().unwrap_or_default();
        let category_name = row.get("CATEGORY_NAME").cloned().unwrap_or_default();
        let category_type = row
            .get("CATEGORY_TYPE")
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_else(|| "expense".to_string());
        let match_field = row
            .get("MATCH_FIELD")
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_else(|| "description".to_string());
        let is_regex = matches!(
            row.get("IS_REGEX").map(String::as_str).unwrap_or("0").trim(),
            "1" | "true" | "True"
        );
        let priority = row
            .get("PRIORITY")
            .and_then(|s| s.trim().parse::<i64>().ok())
            .unwrap_or(0);

        if pattern.trim().is_empty() || category_name.trim().is_empty() {
            skipped += 1;
            continue;
        }

        let category_id = match category_map
            .get(&normalize_key(&[&category_name, &category_type]))
            .copied()
        {
            Some(id) => id,
            None => {
                skipped += 1;
                warnings.push(format!(
                    "Rule row {} skipped: category {} ({}) not found",
                    idx + 2,
                    category_name,
                    category_type
                ));
                continue;
            }
        };

        let existing = conn
            .query_row(
                "SELECT id FROM category_rules WHERE category_id = ?1 AND pattern = ?2 AND match_field = ?3 AND is_regex = ?4",
                params![category_id, pattern, match_field, is_regex as i64],
                |row| row.get::<_, i64>(0),
            )
            .ok();

        if let Some(rule_id) = existing {
            conn.execute(
                "UPDATE category_rules SET priority = ?1 WHERE id = ?2",
                params![priority, rule_id],
            )
            .map_err(|e| e.to_string())?;
            updated += 1;
        } else {
            conn.execute(
                "INSERT INTO category_rules (category_id, pattern, match_field, is_regex, priority) VALUES (?1, ?2, ?3, ?4, ?5)",
                params![category_id, pattern, match_field, is_regex as i64, priority],
            )
            .map_err(|e| e.to_string())?;
            inserted += 1;
        }
    }

    Ok((inserted, updated, skipped, warnings))
}

fn import_transactions_rows(
    conn: &rusqlite::Connection,
    rows: &[HashMap<String, String>],
    account_map: &mut HashMap<String, i64>,
    category_map: &HashMap<String, i64>,
) -> Result<(usize, usize, usize, Vec<String>), String> {
    let mut imported = 0;
    let mut updated = 0;
    let mut skipped = 0;
    let mut warnings = Vec::new();
    let mut touched_accounts: HashSet<i64> = HashSet::new();

    for (idx, row) in rows.iter().enumerate() {
        let account_name = row.get("ACCOUNT_NAME").cloned().unwrap_or_default();
        if account_name.trim().is_empty() {
            skipped += 1;
            warnings.push(format!("Transaction row {} skipped: ACCOUNT_NAME is required", idx + 2));
            continue;
        }

        let account_currency = normalize_currency(
            row.get("ACCOUNT_CURRENCY")
                .map(String::as_str)
                .unwrap_or("USD"),
        );
        let account_type = row
            .get("ACCOUNT_TYPE")
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_else(|| "checking".to_string());

        let account_key = normalize_key(&[&account_name, &account_currency, &account_type]);
        let account_id = if let Some(id) = account_map.get(&account_key).copied() {
            id
        } else {
            conn.execute(
                "INSERT INTO accounts (name, account_type, balance, currency) VALUES (?1, ?2, 0, ?3)",
                params![account_name, account_type, account_currency],
            )
            .map_err(|e| e.to_string())?;
            let id = conn.last_insert_rowid();
            account_map.insert(account_key, id);
            id
        };

        let date = row.get("DATE").cloned().unwrap_or_default();
        let raw_amount = row.get("AMOUNT").cloned().unwrap_or_else(|| "0".to_string());
        let amount = match raw_amount.replace(',', ".").parse::<f64>() {
            Ok(v) => v,
            Err(_) => {
                skipped += 1;
                warnings.push(format!("Transaction row {} skipped: invalid AMOUNT", idx + 2));
                continue;
            }
        };

        let mut transaction_type = row
            .get("TRANSACTION_TYPE")
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_default();
        if !["income", "expense", "transfer"].contains(&transaction_type.as_str()) {
            transaction_type = if amount < 0.0 {
                "expense".to_string()
            } else {
                "income".to_string()
            };
        }

        let normalized_amount = amount.abs();
        let description = row.get("DESCRIPTION").cloned().unwrap_or_default();
        let payee = row.get("PAYEE").cloned().unwrap_or_default();
        let category_name = row.get("CATEGORY").cloned().unwrap_or_default();
        let category_type = row
            .get("CATEGORY_TYPE")
            .map(|s| s.trim().to_lowercase())
            .unwrap_or_default();

        let category_id = if !category_name.trim().is_empty() {
            if let Some(id) = category_map
                .get(&normalize_key(&[&category_name, &category_type]))
                .copied()
            {
                Some(id)
            } else {
                category_map
                    .iter()
                    .find_map(|(key, id)| {
                        let first = key.split("||").next().unwrap_or("");
                        if first == category_name.trim().to_lowercase() {
                            Some(*id)
                        } else {
                            None
                        }
                    })
            }
        } else {
            None
        };

        let id_transaction = row.get("ID_TRANSACTION").cloned().unwrap_or_default();
        let stable_id = if id_transaction.trim().is_empty() {
            stable_transaction_id(
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
        } else {
            conn.execute(
                "INSERT INTO transactions (account_id, category_id, transaction_type, amount, description, payee, date, id_transaction) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
                params![account_id, category_id, transaction_type, normalized_amount, description, payee, date, stable_id],
            )
            .map_err(|e| e.to_string())?;
            imported += 1;
        }

        touched_accounts.insert(account_id);
    }

    for account_id in touched_accounts {
        recalc_balance_for_account(conn, account_id)?;
    }

    Ok((imported, updated, skipped, warnings))
}

fn extract_sheet_csv_from_xlsx(xlsx_bytes: &[u8], sheet_name: &str) -> Result<Option<String>, String> {
    let mut workbook = open_workbook_auto_from_rs(Cursor::new(xlsx_bytes.to_vec())).map_err(|e| e.to_string())?;
    let range = match workbook.worksheet_range(sheet_name) {
        Ok(r) => r,
        Err(_) => return Ok(None),
    };

    let mut writer = Writer::from_writer(Vec::new());
    for row in range.rows() {
        let values = row.iter().map(|cell| cell.to_string()).collect::<Vec<_>>();
        writer.write_record(values).map_err(|e| e.to_string())?;
    }
    writer.flush().map_err(|e| e.to_string())?;
    let bytes = writer.into_inner().map_err(|e| e.to_string())?;
    let csv_text = String::from_utf8(bytes).map_err(|e| e.to_string())?;

    if csv_text.trim().is_empty() {
        Ok(None)
    } else {
        Ok(Some(csv_text))
    }
}

#[tauri::command]
pub fn export_full_backup_zip_base64(state: tauri::State<AppState>) -> Result<String, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let accounts_csv = export_accounts_csv(conn)?;
    let categories_csv = export_categories_csv(conn)?;
    let rules_csv = export_rules_csv(conn)?;
    let transactions_csv = export_transactions_csv(conn)?;

    let mut grouped_transactions: HashMap<String, Vec<HashMap<String, String>>> = HashMap::new();
    for row in parse_dict_rows(&transactions_csv)? {
        let account_name = row
            .get("ACCOUNT_NAME")
            .cloned()
            .unwrap_or_else(|| "account".to_string());
        grouped_transactions.entry(account_name).or_default().push(row);
    }

    let mut zip_buffer = Cursor::new(Vec::new());
    {
        let mut zip = ZipWriter::new(&mut zip_buffer);
        let options = SimpleFileOptions::default();

        zip.start_file("accounts.csv", options).map_err(|e| e.to_string())?;
        zip.write_all(accounts_csv.as_bytes()).map_err(|e| e.to_string())?;

        zip.start_file("categories.csv", options).map_err(|e| e.to_string())?;
        zip.write_all(categories_csv.as_bytes()).map_err(|e| e.to_string())?;

        zip.start_file("rules.csv", options).map_err(|e| e.to_string())?;
        zip.write_all(rules_csv.as_bytes()).map_err(|e| e.to_string())?;

        zip.start_file("transactions.csv", options).map_err(|e| e.to_string())?;
        zip.write_all(transactions_csv.as_bytes()).map_err(|e| e.to_string())?;

        for (account_name, rows) in grouped_transactions {
            let mut writer = Writer::from_writer(Vec::new());
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
            for row in rows {
                writer
                    .write_record([
                        row.get("ID_TRANSACTION").cloned().unwrap_or_default(),
                        row.get("DATE").cloned().unwrap_or_default(),
                        row.get("AMOUNT").cloned().unwrap_or_default(),
                        row.get("CATEGORY").cloned().unwrap_or_default(),
                        row.get("DESCRIPTION").cloned().unwrap_or_default(),
                        row.get("PAYEE").cloned().unwrap_or_default(),
                        row.get("ACCOUNT_NAME").cloned().unwrap_or_default(),
                        row.get("ACCOUNT_CURRENCY").cloned().unwrap_or_default(),
                        row.get("TRANSACTION_TYPE").cloned().unwrap_or_default(),
                    ])
                    .map_err(|e| e.to_string())?;
            }
            writer.flush().map_err(|e| e.to_string())?;
            let bytes = writer.into_inner().map_err(|e| e.to_string())?;

            let filename = format!("transactions_{}.csv", slugify(&account_name));
            zip.start_file(filename, options).map_err(|e| e.to_string())?;
            zip.write_all(&bytes).map_err(|e| e.to_string())?;
        }

        let manifest = serde_json::json!({
            "format_version": "1.0",
            "datasets": ["accounts", "categories", "rules", "transactions"],
            "transactions_count": parse_dict_rows(&transactions_csv)?.len(),
        });
        zip.start_file("manifest.json", options).map_err(|e| e.to_string())?;
        zip.write_all(manifest.to_string().as_bytes())
            .map_err(|e| e.to_string())?;

        zip.finish().map_err(|e| e.to_string())?;
    }

    Ok(general_purpose::STANDARD.encode(zip_buffer.into_inner()))
}

#[tauri::command]
pub fn export_full_backup_xlsx_base64(state: tauri::State<AppState>) -> Result<String, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let accounts_csv = export_accounts_csv(conn)?;
    let categories_csv = export_categories_csv(conn)?;
    let rules_csv = export_rules_csv(conn)?;
    let transactions_csv = export_transactions_csv(conn)?;

    let mut workbook = Workbook::new();

    {
        let mut metadata = workbook.add_worksheet();
        metadata.set_name("Metadata").map_err(|e| e.to_string())?;
        metadata.write_string(0, 0, "KEY").map_err(|e| e.to_string())?;
        metadata.write_string(0, 1, "VALUE").map_err(|e| e.to_string())?;
        metadata
            .write_string(1, 0, "format_version")
            .map_err(|e| e.to_string())?;
        metadata.write_string(1, 1, "1.0").map_err(|e| e.to_string())?;
        metadata
            .write_string(2, 0, "transactions_count")
            .map_err(|e| e.to_string())?;
        metadata
            .write_number(2, 1, parse_dict_rows(&transactions_csv)?.len() as f64)
            .map_err(|e| e.to_string())?;
    }

    write_sheet_from_csv(&mut workbook, "Accounts", &accounts_csv)?;
    write_sheet_from_csv(&mut workbook, "Categories", &categories_csv)?;
    write_sheet_from_csv(&mut workbook, "Rules", &rules_csv)?;
    write_sheet_from_csv(&mut workbook, "Transactions", &transactions_csv)?;

    let bytes = workbook.save_to_buffer().map_err(|e| e.to_string())?;
    Ok(general_purpose::STANDARD.encode(bytes))
}

#[tauri::command]
pub fn import_full_backup_zip_base64(
    state: tauri::State<AppState>,
    archive_base64: String,
) -> Result<FullBackupImportResult, String> {
    let bytes = general_purpose::STANDARD
        .decode(archive_base64)
        .map_err(|e| e.to_string())?;

    let mut archive = ZipArchive::new(Cursor::new(bytes)).map_err(|e| e.to_string())?;

    let mut accounts_csv = String::new();
    let mut categories_csv = String::new();
    let mut rules_csv = String::new();
    let mut transaction_csv_chunks: Vec<String> = Vec::new();

    for i in 0..archive.len() {
        let mut file = archive.by_index(i).map_err(|e| e.to_string())?;
        let name = file.name().to_lowercase();
        let mut content = String::new();
        file.read_to_string(&mut content).map_err(|e| e.to_string())?;

        if name == "accounts.csv" {
            accounts_csv = content;
        } else if name == "categories.csv" {
            categories_csv = content;
        } else if name == "rules.csv" {
            rules_csv = content;
        } else if name == "transactions.csv" || (name.starts_with("transactions_") && name.ends_with(".csv")) {
            transaction_csv_chunks.push(content);
        }
    }

    import_full_payload(
        state,
        &accounts_csv,
        &categories_csv,
        &rules_csv,
        &transaction_csv_chunks,
    )
}

#[tauri::command]
pub fn import_full_backup_xlsx_base64(
    state: tauri::State<AppState>,
    workbook_base64: String,
) -> Result<FullBackupImportResult, String> {
    let bytes = general_purpose::STANDARD
        .decode(workbook_base64)
        .map_err(|e| e.to_string())?;

    let accounts_csv = extract_sheet_csv_from_xlsx(&bytes, "Accounts")?.unwrap_or_default();
    let categories_csv = extract_sheet_csv_from_xlsx(&bytes, "Categories")?.unwrap_or_default();
    let rules_csv = extract_sheet_csv_from_xlsx(&bytes, "Rules")?.unwrap_or_default();
    let transactions_csv = extract_sheet_csv_from_xlsx(&bytes, "Transactions")?.unwrap_or_default();

    import_full_payload(
        state,
        &accounts_csv,
        &categories_csv,
        &rules_csv,
        &[transactions_csv],
    )
}

fn import_full_payload(
    state: tauri::State<AppState>,
    accounts_csv: &str,
    categories_csv: &str,
    rules_csv: &str,
    transactions_csv_chunks: &[String],
) -> Result<FullBackupImportResult, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let account_rows = if accounts_csv.trim().is_empty() {
        Vec::new()
    } else {
        parse_dict_rows(accounts_csv)?
    };
    let category_rows = if categories_csv.trim().is_empty() {
        Vec::new()
    } else {
        parse_dict_rows(categories_csv)?
    };
    let rule_rows = if rules_csv.trim().is_empty() {
        Vec::new()
    } else {
        parse_dict_rows(rules_csv)?
    };

    let mut tx_rows = Vec::new();
    for chunk in transactions_csv_chunks {
        if chunk.trim().is_empty() {
            continue;
        }
        let mut parsed = parse_dict_rows(chunk)?;
        tx_rows.append(&mut parsed);
    }

    let (mut account_map, accounts_inserted, accounts_updated) = import_accounts_rows(conn, &account_rows)?;
    let (category_map, categories_inserted, categories_updated) = import_categories_rows(conn, &category_rows)?;
    let (rules_inserted, rules_updated, rules_skipped, mut rule_warnings) =
        import_rules_rows(conn, &rule_rows, &category_map)?;
    let (transactions_imported, transactions_updated, tx_skipped, mut tx_warnings) =
        import_transactions_rows(conn, &tx_rows, &mut account_map, &category_map)?;

    let mut warnings = Vec::new();
    warnings.append(&mut rule_warnings);
    warnings.append(&mut tx_warnings);

    Ok(FullBackupImportResult {
        accounts_inserted,
        accounts_updated,
        categories_inserted,
        categories_updated,
        rules_inserted,
        rules_updated,
        transactions_imported,
        transactions_updated,
        skipped: rules_skipped + tx_skipped,
        warnings,
    })
}
