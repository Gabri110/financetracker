use crate::AppState;
use rusqlite::params;
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Account {
    pub id: i64,
    pub name: String,
    pub account_type: String,
    pub balance: f64,
    pub currency: String,
    pub created_at: String,
}

#[tauri::command]
pub fn get_accounts(state: tauri::State<AppState>) -> Result<Vec<Account>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut stmt = conn
        .prepare("SELECT id, name, account_type, balance, currency, created_at FROM accounts ORDER BY name")
        .map_err(|e| e.to_string())?;

    let accounts = stmt
        .query_map([], |row| {
            Ok(Account {
                id: row.get(0)?,
                name: row.get(1)?,
                account_type: row.get(2)?,
                balance: row.get(3)?,
                currency: row.get(4)?,
                created_at: row.get(5)?,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    Ok(accounts)
}

#[tauri::command]
pub fn add_account(
    state: tauri::State<AppState>,
    name: String,
    account_type: String,
    balance: f64,
    currency: String,
) -> Result<Account, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "INSERT INTO accounts (name, account_type, balance, currency) VALUES (?1, ?2, ?3, ?4)",
        params![name, account_type, balance, currency],
    )
    .map_err(|e| e.to_string())?;

    let id = conn.last_insert_rowid();
    let account = conn
        .query_row(
            "SELECT id, name, account_type, balance, currency, created_at FROM accounts WHERE id = ?1",
            params![id],
            |row| {
                Ok(Account {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    account_type: row.get(2)?,
                    balance: row.get(3)?,
                    currency: row.get(4)?,
                    created_at: row.get(5)?,
                })
            },
        )
        .map_err(|e| e.to_string())?;

    Ok(account)
}

#[tauri::command]
pub fn update_account(
    state: tauri::State<AppState>,
    id: i64,
    name: String,
    account_type: String,
    balance: f64,
    currency: String,
) -> Result<Account, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "UPDATE accounts SET name=?1, account_type=?2, balance=?3, currency=?4 WHERE id=?5",
        params![name, account_type, balance, currency, id],
    )
    .map_err(|e| e.to_string())?;

    let account = conn
        .query_row(
            "SELECT id, name, account_type, balance, currency, created_at FROM accounts WHERE id = ?1",
            params![id],
            |row| {
                Ok(Account {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    account_type: row.get(2)?,
                    balance: row.get(3)?,
                    currency: row.get(4)?,
                    created_at: row.get(5)?,
                })
            },
        )
        .map_err(|e| e.to_string())?;

    Ok(account)
}

#[tauri::command]
pub fn delete_account(state: tauri::State<AppState>, id: i64) -> Result<(), String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute("DELETE FROM accounts WHERE id=?1", params![id])
        .map_err(|e| e.to_string())?;

    Ok(())
}

#[tauri::command]
pub fn adjust_account_balance(
    state: tauri::State<AppState>,
    id: i64,
    target_balance: f64,
) -> Result<Account, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let (current_balance, created_at): (f64, String) = conn
        .query_row(
            "SELECT balance, created_at FROM accounts WHERE id = ?1",
            params![id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| e.to_string())?;

    let rounded_target = (target_balance * 100.0).round() / 100.0;
    let rounded_current = (current_balance * 100.0).round() / 100.0;
    let delta = ((rounded_target - rounded_current) * 100.0).round() / 100.0;

    if delta != 0.0 {
        let transaction_type = if delta > 0.0 { "income" } else { "expense" };
        let amount = delta.abs();
        let adjustment_date = created_at
            .split_whitespace()
            .next()
            .map(str::to_string)
            .unwrap_or_else(|| chrono::Local::now().format("%Y-%m-%d").to_string());

        conn.execute(
            "INSERT INTO transactions (account_id, category_id, transaction_type, amount, description, payee, date)
             VALUES (?1, NULL, ?2, ?3, ?4, ?5, ?6)",
            params![id, transaction_type, amount, "Balance Adjustment", "System Adjustment", adjustment_date],
        )
        .map_err(|e| e.to_string())?;

        conn.execute(
            "UPDATE accounts SET balance = balance + ?1 WHERE id = ?2",
            params![delta, id],
        )
        .map_err(|e| e.to_string())?;
    }

    let account = conn
        .query_row(
            "SELECT id, name, account_type, balance, currency, created_at FROM accounts WHERE id = ?1",
            params![id],
            |row| {
                Ok(Account {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    account_type: row.get(2)?,
                    balance: row.get(3)?,
                    currency: row.get(4)?,
                    created_at: row.get(5)?,
                })
            },
        )
        .map_err(|e| e.to_string())?;

    Ok(account)
}
