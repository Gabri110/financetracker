use crate::AppState;
use crate::commands::fx::{
    convert_amount_from_conn,
    ensure_daily_rates_from_conn,
    get_base_currency_from_conn,
};
use rusqlite::params;
use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Budget {
    pub id: i64,
    pub category_id: i64,
    pub category_name: Option<String>,
    pub category_color: Option<String>,
    pub category_icon: Option<String>,
    pub amount: f64,
    pub period: String,
    pub start_date: String,
    pub spent: f64,
    pub currency: String,
    pub created_at: String,
}

#[tauri::command]
pub fn get_budgets(state: tauri::State<AppState>) -> Result<Vec<Budget>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    ensure_daily_rates_from_conn(conn)?;

    // Get current month range for spending calculation
    let now = chrono::Local::now();
    let month_start = format!("{}-{:02}-01", now.format("%Y"), now.format("%m"));
    let month_end = format!("{}-{:02}-31", now.format("%Y"), now.format("%m"));

    let base_currency = get_base_currency_from_conn(conn)?;

    let mut stmt = conn
        .prepare(
            "SELECT b.id, b.category_id, c.name, c.color, c.icon, b.amount, b.period, b.start_date, b.created_at
             FROM budgets b
             LEFT JOIN categories c ON b.category_id = c.id
             ORDER BY c.name",
        )
        .map_err(|e| e.to_string())?;

    let mut budgets = stmt
        .query_map([], |row| {
            Ok(Budget {
                id: row.get(0)?,
                category_id: row.get(1)?,
                category_name: row.get(2)?,
                category_color: row.get(3)?,
                category_icon: row.get(4)?,
                amount: row.get(5)?,
                period: row.get(6)?,
                start_date: row.get(7)?,
                created_at: row.get(8)?,
                spent: 0.0,
                currency: base_currency.clone(),
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let mut spend_stmt = conn
        .prepare(
            "SELECT category_id, amount, a.currency
             FROM transactions t
             JOIN accounts a ON t.account_id = a.id
             WHERE t.transaction_type = 'expense'
               AND t.date BETWEEN ?1 AND ?2",
        )
        .map_err(|e| e.to_string())?;

    let spend_rows = spend_stmt
        .query_map(params![month_start, month_end], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let mut spent_by_category = std::collections::HashMap::<i64, f64>::new();
    for (category_id, amount, currency) in spend_rows {
        let converted = convert_amount_from_conn(conn, amount, &currency, &base_currency);
        *spent_by_category.entry(category_id).or_insert(0.0) += converted;
    }

    for budget in &mut budgets {
        budget.spent = spent_by_category.get(&budget.category_id).copied().unwrap_or(0.0);
    }

    Ok(budgets)
}

#[tauri::command]
pub fn add_budget(
    state: tauri::State<AppState>,
    category_id: i64,
    amount: f64,
    period: String,
    start_date: String,
) -> Result<Budget, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "INSERT INTO budgets (category_id, amount, period, start_date) VALUES (?1, ?2, ?3, ?4)",
        params![category_id, amount, period, start_date],
    )
    .map_err(|e| e.to_string())?;

    let id = conn.last_insert_rowid();
    fetch_budget(conn, id)
}

#[tauri::command]
pub fn update_budget(
    state: tauri::State<AppState>,
    id: i64,
    category_id: i64,
    amount: f64,
    period: String,
    start_date: String,
) -> Result<Budget, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "UPDATE budgets SET category_id=?1, amount=?2, period=?3, start_date=?4 WHERE id=?5",
        params![category_id, amount, period, start_date, id],
    )
    .map_err(|e| e.to_string())?;

    fetch_budget(conn, id)
}

#[tauri::command]
pub fn delete_budget(state: tauri::State<AppState>, id: i64) -> Result<(), String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute("DELETE FROM budgets WHERE id=?1", params![id])
        .map_err(|e| e.to_string())?;

    Ok(())
}

fn fetch_budget(conn: &rusqlite::Connection, id: i64) -> Result<Budget, String> {
    conn.query_row(
        "SELECT b.id, b.category_id, c.name, c.color, c.icon, b.amount, b.period, b.start_date, b.created_at, 0.0
         FROM budgets b LEFT JOIN categories c ON b.category_id = c.id WHERE b.id = ?1",
        params![id],
        |row| {
            Ok(Budget {
                id: row.get(0)?,
                category_id: row.get(1)?,
                category_name: row.get(2)?,
                category_color: row.get(3)?,
                category_icon: row.get(4)?,
                amount: row.get(5)?,
                period: row.get(6)?,
                start_date: row.get(7)?,
                created_at: row.get(8)?,
                spent: row.get(9)?,
                currency: get_base_currency_from_conn(conn).unwrap_or_else(|_| "USD".to_string()),
            })
        },
    )
    .map_err(|e| e.to_string())
}
