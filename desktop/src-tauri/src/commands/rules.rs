use crate::AppState;
use regex::Regex;
use rusqlite::params;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Rule {
    pub id: i64,
    pub category_id: i64,
    pub category_name: Option<String>,
    pub category_color: Option<String>,
    pub pattern: String,
    pub match_field: String,
    pub is_regex: bool,
    pub priority: i64,
    pub created_at: String,
}

#[derive(Debug, Serialize)]
pub struct RuleImportResult {
    pub inserted: usize,
    pub updated: usize,
    pub skipped: usize,
    pub errors: Vec<String>,
}

fn category_key(name: &str, category_type: &str) -> String {
    format!("{}||{}", name.trim().to_lowercase(), category_type.trim().to_lowercase())
}

#[tauri::command]
pub fn get_rules(state: tauri::State<AppState>) -> Result<Vec<Rule>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut stmt = conn
        .prepare(
            "SELECT r.id, r.category_id, c.name, c.color, r.pattern, r.match_field,
                    r.is_regex, r.priority, r.created_at
             FROM category_rules r
             LEFT JOIN categories c ON r.category_id = c.id
             ORDER BY r.priority DESC, r.id",
        )
        .map_err(|e| e.to_string())?;

    let rules = stmt
        .query_map([], |row| {
            Ok(Rule {
                id: row.get(0)?,
                category_id: row.get(1)?,
                category_name: row.get(2)?,
                category_color: row.get(3)?,
                pattern: row.get(4)?,
                match_field: row.get(5)?,
                is_regex: row.get::<_, i64>(6)? != 0,
                priority: row.get(7)?,
                created_at: row.get(8)?,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    Ok(rules)
}

#[tauri::command]
pub fn add_rule(
    state: tauri::State<AppState>,
    category_id: i64,
    pattern: String,
    match_field: String,
    is_regex: bool,
    priority: i64,
) -> Result<Rule, String> {
    if is_regex {
        Regex::new(&pattern).map_err(|e| format!("Invalid regex: {}", e))?;
    }

    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "INSERT INTO category_rules (category_id, pattern, match_field, is_regex, priority)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![category_id, pattern, match_field, is_regex as i64, priority],
    )
    .map_err(|e| e.to_string())?;

    let id = conn.last_insert_rowid();
    fetch_rule(conn, id)
}

#[tauri::command]
pub fn update_rule(
    state: tauri::State<AppState>,
    id: i64,
    category_id: i64,
    pattern: String,
    match_field: String,
    is_regex: bool,
    priority: i64,
) -> Result<Rule, String> {
    if is_regex {
        Regex::new(&pattern).map_err(|e| format!("Invalid regex: {}", e))?;
    }

    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "UPDATE category_rules SET category_id=?1, pattern=?2, match_field=?3, is_regex=?4, priority=?5 WHERE id=?6",
        params![category_id, pattern, match_field, is_regex as i64, priority, id],
    )
    .map_err(|e| e.to_string())?;

    fetch_rule(conn, id)
}

#[tauri::command]
pub fn delete_rule(state: tauri::State<AppState>, id: i64) -> Result<(), String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute("DELETE FROM category_rules WHERE id=?1", params![id])
        .map_err(|e| e.to_string())?;

    Ok(())
}

#[tauri::command]
pub fn auto_categorize(state: tauri::State<AppState>) -> Result<usize, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    // Load all rules ordered by priority desc
    let mut rule_stmt = conn
        .prepare(
            "SELECT id, category_id, pattern, match_field, is_regex FROM category_rules ORDER BY priority DESC",
        )
        .map_err(|e| e.to_string())?;

    struct RuleRow {
        category_id: i64,
        pattern: String,
        match_field: String,
        is_regex: bool,
    }

    let rules: Vec<RuleRow> = rule_stmt
        .query_map([], |row| {
            Ok(RuleRow {
                category_id: row.get(1)?,
                pattern: row.get(2)?,
                match_field: row.get(3)?,
                is_regex: row.get::<_, i64>(4)? != 0,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    // Load uncategorized transactions
    let mut tx_stmt = conn
        .prepare("SELECT id, description, payee FROM transactions WHERE category_id IS NULL")
        .map_err(|e| e.to_string())?;

    struct TxRow {
        id: i64,
        description: Option<String>,
        payee: Option<String>,
    }

    let txs: Vec<TxRow> = tx_stmt
        .query_map([], |row| {
            Ok(TxRow {
                id: row.get(0)?,
                description: row.get(1)?,
                payee: row.get(2)?,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    let mut count = 0;

    for tx in &txs {
        for rule in &rules {
            let target = match rule.match_field.as_str() {
                "payee" => tx.payee.as_deref().unwrap_or(""),
                _ => tx.description.as_deref().unwrap_or(""),
            };

            let matches = if rule.is_regex {
                Regex::new(&rule.pattern)
                    .map(|re| re.is_match(target))
                    .unwrap_or(false)
            } else {
                target
                    .to_lowercase()
                    .contains(&rule.pattern.to_lowercase())
            };

            if matches {
                conn.execute(
                    "UPDATE transactions SET category_id = ?1 WHERE id = ?2",
                    params![rule.category_id, tx.id],
                )
                .ok();
                count += 1;
                break;
            }
        }
    }

    Ok(count)
}

fn fetch_rule(conn: &rusqlite::Connection, id: i64) -> Result<Rule, String> {
    conn.query_row(
        "SELECT r.id, r.category_id, c.name, c.color, r.pattern, r.match_field,
                r.is_regex, r.priority, r.created_at
         FROM category_rules r LEFT JOIN categories c ON r.category_id = c.id WHERE r.id = ?1",
        params![id],
        |row| {
            Ok(Rule {
                id: row.get(0)?,
                category_id: row.get(1)?,
                category_name: row.get(2)?,
                category_color: row.get(3)?,
                pattern: row.get(4)?,
                match_field: row.get(5)?,
                is_regex: row.get::<_, i64>(6)? != 0,
                priority: row.get(7)?,
                created_at: row.get(8)?,
            })
        },
    )
    .map_err(|e| e.to_string())
}

#[tauri::command]
pub fn export_rules_csv(state: tauri::State<AppState>) -> Result<String, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut writer = csv::Writer::from_writer(Vec::new());
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

#[tauri::command]
pub fn import_rules_csv(state: tauri::State<AppState>, csv_content: String) -> Result<RuleImportResult, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut category_map: HashMap<String, i64> = HashMap::new();
    let mut cat_stmt = conn
        .prepare("SELECT id, name, category_type FROM categories")
        .map_err(|e| e.to_string())?;
    let cat_rows = cat_stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?;
    for row in cat_rows {
        let (id, name, category_type) = row.map_err(|e| e.to_string())?;
        category_map.insert(category_key(&name, &category_type), id);
    }

    let mut reader = csv::ReaderBuilder::new()
        .flexible(true)
        .from_reader(csv_content.as_bytes());
    let headers = reader.headers().map_err(|e| e.to_string())?.clone();
    let header_index = |name: &str| {
        headers
            .iter()
            .position(|h| h.trim().eq_ignore_ascii_case(name))
    };

    let pattern_idx = header_index("PATTERN").ok_or("Missing PATTERN column")?;
    let cat_name_idx = header_index("CATEGORY_NAME").ok_or("Missing CATEGORY_NAME column")?;
    let cat_type_idx = header_index("CATEGORY_TYPE").ok_or("Missing CATEGORY_TYPE column")?;
    let match_field_idx = header_index("MATCH_FIELD").ok_or("Missing MATCH_FIELD column")?;
    let is_regex_idx = header_index("IS_REGEX").ok_or("Missing IS_REGEX column")?;
    let priority_idx = header_index("PRIORITY").ok_or("Missing PRIORITY column")?;

    let mut inserted = 0;
    let mut updated = 0;
    let mut skipped = 0;
    let mut errors = Vec::new();

    for (row_num, result) in reader.records().enumerate() {
        let record = match result {
            Ok(r) => r,
            Err(e) => {
                skipped += 1;
                errors.push(format!("Row {}: {}", row_num + 2, e));
                continue;
            }
        };

        let pattern = record.get(pattern_idx).unwrap_or("").trim().to_string();
        let category_name = record.get(cat_name_idx).unwrap_or("").trim().to_string();
        let category_type = record.get(cat_type_idx).unwrap_or("").trim().to_lowercase();
        let match_field = record
            .get(match_field_idx)
            .unwrap_or("description")
            .trim()
            .to_lowercase();
        let is_regex = matches!(
            record.get(is_regex_idx).unwrap_or("0").trim(),
            "1" | "true" | "True"
        );
        let priority = record
            .get(priority_idx)
            .unwrap_or("0")
            .trim()
            .parse::<i64>()
            .unwrap_or(0);

        if pattern.is_empty() || category_name.is_empty() {
            skipped += 1;
            continue;
        }

        if is_regex {
            if let Err(e) = Regex::new(&pattern) {
                skipped += 1;
                errors.push(format!("Row {}: invalid regex {}", row_num + 2, e));
                continue;
            }
        }

        let category_id = match category_map.get(&category_key(&category_name, &category_type)).copied() {
            Some(id) => id,
            None => {
                skipped += 1;
                errors.push(format!(
                    "Row {}: category not found for {} ({})",
                    row_num + 2,
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

    Ok(RuleImportResult {
        inserted,
        updated,
        skipped,
        errors,
    })
}
