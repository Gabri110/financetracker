use crate::AppState;
use regex::Regex;
use rusqlite::params;
use serde::Serialize;
use std::collections::HashMap;

const ECB_DAILY_URL: &str = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml";

#[derive(Debug, Serialize)]
pub struct ExchangeRate {
    pub from_currency: String,
    pub to_currency: String,
    pub rate: f64,
    pub provider: String,
    pub updated_at: String,
}

#[derive(Debug, Serialize)]
pub struct SyncResult {
    pub base_currency: String,
    pub synced_pairs: usize,
    pub provider: String,
}

fn normalize_currency(currency: &str) -> String {
    currency.trim().to_uppercase()
}

pub fn get_base_currency_from_conn(conn: &rusqlite::Connection) -> Result<String, String> {
    conn.query_row(
        "SELECT value FROM app_settings WHERE key = 'base_currency' LIMIT 1",
        [],
        |row| row.get::<_, String>(0),
    )
    .map(|v| v.trim().to_uppercase())
    .or_else(|_| Ok("USD".to_string()))
}

fn fetch_ecb_rates_against_eur() -> Result<HashMap<String, f64>, String> {
    let body = reqwest::blocking::get(ECB_DAILY_URL)
        .map_err(|e| format!("ECB request failed: {e}"))?
        .text()
        .map_err(|e| format!("ECB body read failed: {e}"))?;

    let pattern = Regex::new(r#"currency=['\"]([A-Z]{3})['\"]\s+rate=['\"]([0-9]+(?:\.[0-9]+)?)['\"]"#)
        .map_err(|e| format!("Rate regex init failed: {e}"))?;

    let mut eur_pairs = HashMap::new();
    eur_pairs.insert("EUR".to_string(), 1.0);

    for captures in pattern.captures_iter(&body) {
        let code = captures
            .get(1)
            .map(|m| m.as_str().to_string())
            .unwrap_or_default();
        let rate_str = captures.get(2).map(|m| m.as_str()).unwrap_or("0");
        let parsed = rate_str.parse::<f64>().unwrap_or(0.0);
        if parsed > 0.0 {
            eur_pairs.insert(code, parsed);
        }
    }

    if eur_pairs.len() < 2 {
        return Err("ECB response did not contain enough rates".to_string());
    }

    Ok(eur_pairs)
}

fn sync_ecb_rates_for_base_conn(conn: &rusqlite::Connection, base_currency: &str) -> Result<usize, String> {
    let base = normalize_currency(base_currency);
    let eur_pairs = fetch_ecb_rates_against_eur()?;

    let eur_to_base = eur_pairs
        .get(&base)
        .copied()
        .ok_or_else(|| format!("ECB feed is missing base currency {base}"))?;

    let mut synced = 0usize;
    for (from, eur_to_from) in eur_pairs {
        if eur_to_from <= 0.0 {
            continue;
        }
        let rate_from_to_base = if from == base {
            1.0
        } else {
            eur_to_base / eur_to_from
        };

        conn.execute(
            "INSERT INTO exchange_rates (from_currency, to_currency, rate, provider, updated_at)
             VALUES (?1, ?2, ?3, 'ecb', datetime('now'))
             ON CONFLICT(from_currency, to_currency)
             DO UPDATE SET rate = excluded.rate, provider = excluded.provider, updated_at = datetime('now')",
            params![from, base, rate_from_to_base],
        )
        .map_err(|e| e.to_string())?;
        synced += 1;
    }

    Ok(synced)
}

pub fn ensure_daily_rates_from_conn(conn: &rusqlite::Connection) -> Result<(), String> {
    let base = get_base_currency_from_conn(conn)?;

    let has_today = conn
        .query_row(
            "SELECT 1 FROM exchange_rates
             WHERE to_currency = ?1 AND date(updated_at) = date('now', 'localtime')
             LIMIT 1",
            params![base],
            |_| Ok(()),
        )
        .is_ok();

    if !has_today {
        let _ = sync_ecb_rates_for_base_conn(conn, &base);
    }

    Ok(())
}

pub fn convert_amount_from_conn(
    conn: &rusqlite::Connection,
    amount: f64,
    from_currency: &str,
    to_currency: &str,
) -> f64 {
    let from = from_currency.trim().to_uppercase();
    let to = to_currency.trim().to_uppercase();

    if from == to {
        return amount;
    }

    let direct = conn
        .query_row(
            "SELECT rate FROM exchange_rates
             WHERE from_currency = ?1 AND to_currency = ?2
             ORDER BY updated_at DESC LIMIT 1",
            params![from, to],
            |row| row.get::<_, f64>(0),
        )
        .ok();

    if let Some(rate) = direct {
        return amount * rate;
    }

    let inverse = conn
        .query_row(
            "SELECT rate FROM exchange_rates
             WHERE from_currency = ?1 AND to_currency = ?2
             ORDER BY updated_at DESC LIMIT 1",
            params![to, from],
            |row| row.get::<_, f64>(0),
        )
        .ok();

    if let Some(rate) = inverse {
        if rate != 0.0 {
            return amount / rate;
        }
    }

    amount
}

#[tauri::command]
pub fn get_base_currency(state: tauri::State<AppState>) -> Result<String, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;
    get_base_currency_from_conn(conn)
}

#[tauri::command]
pub fn set_base_currency(
    state: tauri::State<AppState>,
    currency: String,
) -> Result<String, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let normalized = currency.trim().to_uppercase();
    if normalized.is_empty() {
        return Err("Currency cannot be empty".to_string());
    }

    conn.execute(
        "INSERT INTO app_settings (key, value, updated_at)
         VALUES ('base_currency', ?1, datetime('now'))
         ON CONFLICT(key)
         DO UPDATE SET value=excluded.value, updated_at=datetime('now')",
        params![normalized],
    )
    .map_err(|e| e.to_string())?;

    Ok(normalized)
}

#[tauri::command]
pub fn sync_ecb_rates(state: tauri::State<AppState>) -> Result<SyncResult, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;
    let base = get_base_currency_from_conn(conn)?;
    let synced_pairs = sync_ecb_rates_for_base_conn(conn, &base)?;

    Ok(SyncResult {
        base_currency: base,
        synced_pairs,
        provider: "ecb".to_string(),
    })
}

#[tauri::command]
pub fn get_exchange_rates(
    state: tauri::State<AppState>,
    to_currency: Option<String>,
) -> Result<Vec<ExchangeRate>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;
    let target = normalize_currency(
        to_currency
            .as_deref()
            .unwrap_or(&get_base_currency_from_conn(conn)?),
    );

    let mut stmt = conn
        .prepare(
            "SELECT from_currency, to_currency, rate, provider, updated_at
             FROM exchange_rates
             WHERE to_currency = ?1
             ORDER BY CASE WHEN from_currency = ?1 THEN 0 ELSE 1 END, from_currency ASC",
        )
        .map_err(|e| e.to_string())?;

    stmt.query_map(params![target], |row| {
        Ok(ExchangeRate {
            from_currency: row.get(0)?,
            to_currency: row.get(1)?,
            rate: row.get(2)?,
            provider: row.get(3)?,
            updated_at: row.get(4)?,
        })
    })
    .map_err(|e| e.to_string())?
    .collect::<Result<Vec<_>, _>>()
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn set_exchange_rate(
    state: tauri::State<AppState>,
    from_currency: String,
    to_currency: String,
    rate: f64,
) -> Result<ExchangeRate, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    if rate <= 0.0 {
        return Err("Rate must be greater than 0".to_string());
    }

    let from = normalize_currency(&from_currency);
    let to = normalize_currency(&to_currency);

    conn.execute(
        "INSERT INTO exchange_rates (from_currency, to_currency, rate, provider, updated_at)
         VALUES (?1, ?2, ?3, 'manual', datetime('now'))
         ON CONFLICT(from_currency, to_currency)
         DO UPDATE SET rate = excluded.rate, provider = excluded.provider, updated_at = datetime('now')",
        params![from, to, rate],
    )
    .map_err(|e| e.to_string())?;

    let updated_at = conn
        .query_row(
            "SELECT updated_at FROM exchange_rates WHERE from_currency = ?1 AND to_currency = ?2",
            params![from, to],
            |row| row.get::<_, String>(0),
        )
        .map_err(|e| e.to_string())?;

    Ok(ExchangeRate {
        from_currency: from,
        to_currency: to,
        rate,
        provider: "manual".to_string(),
        updated_at,
    })
}
