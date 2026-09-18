import sqlite3

# 1. Create and connect to the central database file
conn = sqlite3.connect("financial_intelligence.db")
cursor = conn.cursor()

# Enforce rules between tables
cursor.execute("PRAGMA foreign_keys = ON;")

# 2. Initialize Table 1: The Company Address Book
cursor.execute("""
CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    company_name TEXT,
    sector TEXT,
    index_category TEXT
);
""")

# 3. Initialize Table 2: The Vertical Financial Vault
cursor.execute("""
CREATE TABLE IF NOT EXISTS financial_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT,
    statement_type TEXT,
    period_date TEXT,
    metric_slug TEXT,
    original_metric_name TEXT,
    financial_value REAL,
    FOREIGN KEY(ticker) REFERENCES companies(ticker)
);
""")
conn.commit()