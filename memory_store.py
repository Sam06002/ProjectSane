import sqlite3
import os
import json

DB_PATH = "logs/sane_memory.db"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS resolutions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_summary TEXT,
            odoo_module TEXT,
            odoo_version TEXT,
            error_message TEXT,
            root_cause TEXT,
            resolution_steps TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS navigation_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_name TEXT,
            url_structure TEXT
        )
    ''')
    conn.commit()
    conn.close()

def search_similar_resolutions(odoo_module: str, error_message: str) -> list:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Simple keyword search on module or error message
    cursor.execute('''
        SELECT * FROM resolutions 
        WHERE odoo_module LIKE ? OR error_message LIKE ? OR ticket_summary LIKE ?
        ORDER BY id DESC LIMIT 5
    ''', (f"%{odoo_module}%", f"%{error_message}%", f"%{error_message}%"))
    
    rows = cursor.fetchall()
    conn.close()
    
    results = []
    for row in rows:
        results.append({
            "ticket_summary": row["ticket_summary"],
            "odoo_module": row["odoo_module"],
            "odoo_version": row["odoo_version"],
            "error_message": row["error_message"],
            "root_cause": row["root_cause"],
            "resolution_steps": json.loads(row["resolution_steps"]) if row["resolution_steps"] else []
        })
    return results

def save_resolution(ticket_summary: str, odoo_module: str, odoo_version: str, error_message: str, root_cause: str, resolution_steps: list) -> None:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO resolutions (ticket_summary, odoo_module, odoo_version, error_message, root_cause, resolution_steps)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (ticket_summary, odoo_module, odoo_version, error_message, root_cause, json.dumps(resolution_steps)))
    conn.commit()
    conn.close()

def get_all_navigation_patterns() -> list:
    init_db()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM navigation_patterns')
    rows = cursor.fetchall()
    conn.close()
    
    return [{"pattern_name": row["pattern_name"], "url_structure": row["url_structure"]} for row in rows]
