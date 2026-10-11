import os
from pathlib import Path
from dotenv import load_dotenv
import psycopg2

load_dotenv(Path(__file__).parent / ".env")

SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")


def get_connection():
    """Returns a new connection to the Supabase Postgres database."""
    if not SUPABASE_DB_URL:
        raise ValueError("SUPABASE_DB_URL not set. Add it to your .env file.")
    return psycopg2.connect(SUPABASE_DB_URL)


def ensure_schema():
    """Creates tables/indexes if missing. Safe to call on every run."""
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Company Address Book
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS companies (
        ticker TEXT PRIMARY KEY,
        company_name TEXT,
        sector TEXT,
        index_category TEXT
    );
    """)

    # 2. Vertical Financial Vault
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS financial_records (
        record_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        ticker TEXT REFERENCES companies(ticker),
        statement_type TEXT,
        period_date TEXT,
        metric_slug TEXT,
        original_metric_name TEXT,
        financial_value DOUBLE PRECISION
    );
    """)

    # One value per (ticker, statement_type, period_date, metric_slug).
    # Lets repeated scrapes UPSERT instead of duplicating rows.
    cursor.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_financial_records_unique
    ON financial_records (ticker, statement_type, period_date, metric_slug);
    """)

    conn.commit()
    cursor.close()
    conn.close()


if __name__ == "__main__":
    ensure_schema()
    print("Schema ready on Supabase.")
