use crate::AppState;
use rusqlite::params;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct Category {
    pub id: i64,
    pub name: String,
    pub category_type: String,
    pub icon: String,
    pub color: String,
    pub created_at: String,
}

#[derive(Debug, Serialize)]
pub struct CategoryImportResult {
    pub inserted: usize,
    pub updated: usize,
    pub skipped: usize,
    pub errors: Vec<String>,
}

fn category_key(name: &str, category_type: &str) -> String {
    format!("{}||{}", name.trim().to_lowercase(), category_type.trim().to_lowercase())
}

#[tauri::command]
pub fn get_categories(state: tauri::State<AppState>) -> Result<Vec<Category>, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut stmt = conn
        .prepare("SELECT id, name, category_type, icon, color, created_at FROM categories ORDER BY category_type, name")
        .map_err(|e| e.to_string())?;

    let categories = stmt
        .query_map([], |row| {
            Ok(Category {
                id: row.get(0)?,
                name: row.get(1)?,
                category_type: row.get(2)?,
                icon: row.get(3)?,
                color: row.get(4)?,
                created_at: row.get(5)?,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;

    Ok(categories)
}

#[tauri::command]
pub fn add_category(
    state: tauri::State<AppState>,
    name: String,
    category_type: String,
    icon: String,
    color: String,
) -> Result<Category, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "INSERT INTO categories (name, category_type, icon, color) VALUES (?1, ?2, ?3, ?4)",
        params![name, category_type, icon, color],
    )
    .map_err(|e| e.to_string())?;

    let id = conn.last_insert_rowid();
    let category = conn
        .query_row(
            "SELECT id, name, category_type, icon, color, created_at FROM categories WHERE id = ?1",
            params![id],
            |row| {
                Ok(Category {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    category_type: row.get(2)?,
                    icon: row.get(3)?,
                    color: row.get(4)?,
                    created_at: row.get(5)?,
                })
            },
        )
        .map_err(|e| e.to_string())?;

    Ok(category)
}

#[tauri::command]
pub fn update_category(
    state: tauri::State<AppState>,
    id: i64,
    name: String,
    category_type: String,
    icon: String,
    color: String,
) -> Result<Category, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute(
        "UPDATE categories SET name=?1, category_type=?2, icon=?3, color=?4 WHERE id=?5",
        params![name, category_type, icon, color, id],
    )
    .map_err(|e| e.to_string())?;

    let category = conn
        .query_row(
            "SELECT id, name, category_type, icon, color, created_at FROM categories WHERE id = ?1",
            params![id],
            |row| {
                Ok(Category {
                    id: row.get(0)?,
                    name: row.get(1)?,
                    category_type: row.get(2)?,
                    icon: row.get(3)?,
                    color: row.get(4)?,
                    created_at: row.get(5)?,
                })
            },
        )
        .map_err(|e| e.to_string())?;

    Ok(category)
}

#[tauri::command]
pub fn delete_category(state: tauri::State<AppState>, id: i64) -> Result<(), String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    conn.execute("DELETE FROM categories WHERE id=?1", params![id])
        .map_err(|e| e.to_string())?;

    Ok(())
}

#[tauri::command]
pub fn export_categories_csv(state: tauri::State<AppState>) -> Result<String, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut writer = csv::Writer::from_writer(Vec::new());
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

#[tauri::command]
pub fn import_categories_csv(
    state: tauri::State<AppState>,
    csv_content: String,
) -> Result<CategoryImportResult, String> {
    let guard = state.db.lock().map_err(|e| e.to_string())?;
    let conn = guard.as_ref().ok_or("No database open")?;

    let mut existing: HashMap<String, i64> = HashMap::new();
    let mut existing_stmt = conn
        .prepare("SELECT id, name, category_type FROM categories")
        .map_err(|e| e.to_string())?;
    let existing_rows = existing_stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map_err(|e| e.to_string())?;
    for row in existing_rows {
        let (id, name, category_type) = row.map_err(|e| e.to_string())?;
        existing.insert(category_key(&name, &category_type), id);
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

    let name_idx = header_index("NAME").ok_or("Missing NAME column")?;
    let type_idx = header_index("CATEGORY_TYPE").ok_or("Missing CATEGORY_TYPE column")?;
    let icon_idx = header_index("ICON");
    let color_idx = header_index("COLOR");

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

        let name = record.get(name_idx).unwrap_or("").trim().to_string();
        let category_type = record.get(type_idx).unwrap_or("").trim().to_lowercase();
        let icon = icon_idx
            .and_then(|idx| record.get(idx))
            .unwrap_or("tag")
            .trim()
            .to_string();
        let color = color_idx
            .and_then(|idx| record.get(idx))
            .unwrap_or("#6c757d")
            .trim()
            .to_string();

        if name.is_empty() || !["income", "expense", "transfer"].contains(&category_type.as_str()) {
            skipped += 1;
            continue;
        }

        let key = category_key(&name, &category_type);
        if let Some(id) = existing.get(&key).copied() {
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
            existing.insert(key, id);
            inserted += 1;
        }
    }

    Ok(CategoryImportResult {
        inserted,
        updated,
        skipped,
        errors,
    })
}
