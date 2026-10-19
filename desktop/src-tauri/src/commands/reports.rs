use crate::AppState;
use crate::commands::fx::{
    convert_amount_from_conn,
    get_base_currency_from_conn,
};
use rusqlite::params;
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct DashboardStats {
    pub total_balance: f64,
    pub monthly_income: f64,
    pub monthly_expenses: f64,
    pub account_count: i64,
    pub transaction_count: i64,
    pub uncategorized_count: i64,
    pub base_currency: String,
}

#[derive(Debug, Serialize)]
pub struct MonthlyData {
    pub month: String,
    pub income: f64,
    pub expenses: f64,
}

#[derive(Debug, Serialize)]
pub struct CategorySpending {
    pub category_name: String,
    pub category_color: String,
    pub category_icon: String,
    pub total: f64,
}

#[derive(Debug, Serialize)]
pub struct TopPayee {
    pub payee: String,
    pub total: f64,
    pub count: i64,
}

#[derive(Debug, Serialize)]
pub struct RecentTransaction {
    pub id: i64,
    pub description: Option<String>,
    pub payee: Option<String>,
    pub amount: f64,
    pub transaction_type: String,
    pub category_name: Option<String>,
    pub category_color: Option<String>,
    pub account_name: Option<String>,
    pub account_currency: Option<String>,
    pub date: String,
}

#[tauri::command]
pub fn get_dashboard_stats(state: tauri::State<AppState>) -> Result<DashboardStats, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let now = chrono::Local::now();
    let month_start = format!("{}-{:02}-01", now.format("%Y"), now.format("%m"));
    let month_end = format!("{}-{:02}-31", now.format("%Y"), now.format("%m"));

    let base_currency = get_base_currency_from_conn(conn)?;

    let mut account_stmt = conn
        .prepare("SELECT balance, currency FROM accounts")
        .map_err(|e| e.to_string())?;
    let account_rows = account_stmt
        .query_map([], |row| Ok((row.get::<_, f64>(0)?, row.get::<_, String>(1)?)))
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let total_balance = account_rows
        .iter()
        .map(|(balance, currency)| convert_amount_from_conn(conn, *balance, currency, &base_currency))
        .sum::<f64>();

    let mut monthly_stmt = conn
        .prepare(
            "SELECT t.amount, t.transaction_type, a.currency
             FROM transactions t
             JOIN accounts a ON t.account_id = a.id
             WHERE t.date BETWEEN ?1 AND ?2",
        )
        .map_err(|e| e.to_string())?;

    let monthly_rows = monthly_stmt
        .query_map(params![month_start, month_end], |row| {
            Ok((
                row.get::<_, f64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let mut monthly_income = 0.0;
    let mut monthly_expenses = 0.0;
    for (amount, tx_type, currency) in monthly_rows {
        let converted = convert_amount_from_conn(conn, amount, &currency, &base_currency);
        if tx_type == "income" {
            monthly_income += converted;
        } else if tx_type == "expense" {
            monthly_expenses += converted;
        }
    }

    let account_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM accounts", [], |r| r.get(0))
        .map_err(|e| e.to_string())?;

    let transaction_count: i64 = conn
        .query_row("SELECT COUNT(*) FROM transactions", [], |r| r.get(0))
        .map_err(|e| e.to_string())?;

    let uncategorized_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM transactions WHERE category_id IS NULL",
            [],
            |r| r.get(0),
        )
        .map_err(|e| e.to_string())?;

    Ok(DashboardStats {
        total_balance,
        monthly_income,
        monthly_expenses,
        account_count,
        transaction_count,
        uncategorized_count,
        base_currency,
    })
}

#[tauri::command]
pub fn get_monthly_data(
    state: tauri::State<AppState>,
    months: u32,
) -> Result<Vec<MonthlyData>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let base_currency = get_base_currency_from_conn(conn)?;

    let mut stmt = conn
        .prepare(
            "SELECT strftime('%Y-%m', t.date) as month,
                    t.transaction_type,
                    t.amount,
                    a.currency
             FROM transactions t
             JOIN accounts a ON t.account_id = a.id
             WHERE t.date >= date('now', ?1)
             ORDER BY month ASC",
        )
        .map_err(|e| e.to_string())?;

    let offset = format!("-{} months", months);
    let rows = stmt
        .query_map(params![offset], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, f64>(2)?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let mut monthly_map = std::collections::BTreeMap::<String, MonthlyData>::new();
    for (month, tx_type, amount, currency) in rows {
        let converted = convert_amount_from_conn(conn, amount, &currency, &base_currency);
        let entry = monthly_map.entry(month.clone()).or_insert(MonthlyData {
            month,
            income: 0.0,
            expenses: 0.0,
        });
        if tx_type == "income" {
            entry.income += converted;
        } else if tx_type == "expense" {
            entry.expenses += converted;
        }
    }

    let data = monthly_map.into_values().collect::<Vec<_>>();

    Ok(data)
}

#[tauri::command]
pub fn get_category_spending(
    state: tauri::State<AppState>,
    period: String,
) -> Result<Vec<CategorySpending>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let base_currency = get_base_currency_from_conn(conn)?;

    let date_filter = match period.as_str() {
        "this_month" => "t.date >= date('now', 'start of month')".to_string(),
        "last_month" => {
            "t.date >= date('now', 'start of month', '-1 month') AND t.date < date('now', 'start of month')".to_string()
        }
        "this_year" => "t.date >= date('now', 'start of year')".to_string(),
        _ => "1=1".to_string(),
    };

    let sql = format!(
        "SELECT c.name, COALESCE(c.color, '#6c757d'), COALESCE(c.icon, 'tag'),
            t.amount, a.currency
         FROM transactions t
         JOIN categories c ON t.category_id = c.id
         JOIN accounts a ON t.account_id = a.id
         WHERE t.transaction_type = 'expense' AND {}
         ORDER BY c.id",
        date_filter
    );

    let mut stmt = conn.prepare(&sql).map_err(|e| e.to_string())?;

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
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let mut by_category = std::collections::HashMap::<String, CategorySpending>::new();
    for (name, color, icon, amount, currency) in rows {
        let converted = convert_amount_from_conn(conn, amount, &currency, &base_currency);
        let entry = by_category.entry(name.clone()).or_insert(CategorySpending {
            category_name: name,
            category_color: color,
            category_icon: icon,
            total: 0.0,
        });
        entry.total += converted;
    }

    let mut data = by_category.into_values().collect::<Vec<_>>();
    data.sort_by(|a, b| b.total.partial_cmp(&a.total).unwrap_or(std::cmp::Ordering::Equal));
    data.truncate(10);

    Ok(data)
}

#[tauri::command]
pub fn get_top_payees(
    state: tauri::State<AppState>,
    limit: u32,
) -> Result<Vec<TopPayee>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let base_currency = get_base_currency_from_conn(conn)?;

    let mut stmt = conn
        .prepare(
            "SELECT COALESCE(t.payee, t.description, 'Unknown') as payee,
                    t.amount,
                    a.currency
             FROM transactions t
             JOIN accounts a ON t.account_id = a.id
             WHERE t.transaction_type = 'expense' AND (t.payee IS NOT NULL OR t.description IS NOT NULL)",
        )
        .map_err(|e| e.to_string())?;

    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let mut payee_map = std::collections::HashMap::<String, TopPayee>::new();
    for (payee, amount, currency) in rows {
        let converted = convert_amount_from_conn(conn, amount, &currency, &base_currency);
        let entry = payee_map.entry(payee.clone()).or_insert(TopPayee {
            payee,
            total: 0.0,
            count: 0,
        });
        entry.total += converted;
        entry.count += 1;
    }

    let mut data = payee_map.into_values().collect::<Vec<_>>();
    data.sort_by(|a, b| b.total.partial_cmp(&a.total).unwrap_or(std::cmp::Ordering::Equal));
    data.truncate(limit as usize);

    Ok(data)
}

#[tauri::command]
pub fn get_recent_transactions(
    state: tauri::State<AppState>,
    limit: u32,
) -> Result<Vec<RecentTransaction>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut stmt = conn
        .prepare(
                "SELECT t.id, t.description, t.payee, t.amount, t.transaction_type,
                    c.name, c.color, a.name, a.currency, t.date
             FROM transactions t
             LEFT JOIN categories c ON t.category_id = c.id
             LEFT JOIN accounts a ON t.account_id = a.id
             ORDER BY t.date DESC, t.id DESC
             LIMIT ?1",
        )
        .map_err(|e| e.to_string())?;

    let data = stmt
        .query_map(params![limit], |row| {
            Ok(RecentTransaction {
                id: row.get(0)?,
                description: row.get(1)?,
                payee: row.get(2)?,
                amount: row.get(3)?,
                transaction_type: row.get(4)?,
                category_name: row.get(5)?,
                category_color: row.get(6)?,
                account_name: row.get(7)?,
                account_currency: row.get(8)?,
                date: row.get(9)?,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    Ok(data)
}
