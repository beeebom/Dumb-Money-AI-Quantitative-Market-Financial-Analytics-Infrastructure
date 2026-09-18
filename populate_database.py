import sqlite3
import pandas as pd
import glob
import os
import re
import json

def make_clean_slug(text):
    """Cleans your Excel row title headers into standard code tokens ('OPM %' -> 'opm_percentage')"""
    text = str(text).lower().strip()
    text = text.replace('%', 'percentage')
    text = re.sub(r'[^a-z0-9_]', '_', text)
    return re.sub(r'_+', '_', text).strip('_')

def fix_date_format(col_name):
    """Standardizes columns like 'Mar 2026' into SQL-friendly 'YYYY-MM-DD'"""
    col_clean = str(col_name).lower().strip()
    match = re.search(r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+(\d{4})', col_clean)
    if match:
        month_map = {
            'jan': '01-31', 'feb': '02-28', 'mar': '03-31', 'apr': '04-30',
            'may': '05-31', 'jun': '06-30', 'jul': '07-31', 'aug': '08-31',
            'sep': '09-30', 'oct': '10-31', 'nov': '11-30', 'dec': '12-31'
        }
        return f"{match.group(2)}-{month_map[match.group(1)]}"
    return col_name

# YOUR EXACT TAB MAPPING DICTIONARY
tab_mapping = {
    'Quarterly Results': 'quarterly_results',
    'Profit Loss': 'profit_loss',
    'Balance Sheet': 'balance_sheet',
    'Cash Flow': 'cash_flow',
    'Ratios Table': 'ratios'
}

def populate_single_stock(file_path):
    """Parses a single Excel file and inserts its data into the database."""
    # 1. Open connection to your existing database file
    conn = sqlite3.connect("financial_intelligence.db")
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    # Extract ticker name from the file name
    filename = os.path.basename(file_path)
    ticker = filename.replace(".xlsx", "").upper().strip()
    
    print(f"\n[Database Pipeline] Processing Workbook: {ticker}")
    
    # 2. Add the company profile line item to Table 1 first
    cursor.execute("""
    INSERT OR IGNORE INTO companies (ticker, company_name, sector, index_category)
    VALUES (?, ?, 'Unassigned Sector', 'Unassigned Index');
    """, (ticker, f"{ticker} Corporate Entity"))

    # 3. Read the Excel file tabs
    try:
        excel_file = pd.ExcelFile(file_path)
    except Exception as e:
        print(f"  -> Skipping {filename} due to read error (likely corrupted or empty).")
        conn.close()
        return
    
    for tab_name in excel_file.sheet_names:
        if tab_name not in tab_mapping:
            continue # Safely skips unmapped or empty tabs
            
        statement_slug = tab_mapping[tab_name]
        print(f"  -> Extracting cells from tab: '{tab_name}' into database code category: '{statement_slug}'")
        
        # Open the active tab
        df = pd.read_excel(file_path, sheet_name=tab_name)
        
        if 'Metric' not in df.columns:
            df.rename(columns={df.columns[0]: 'Metric'}, inplace=True)
            
        time_columns = [col for col in df.columns if col != 'Metric' and not str(col).startswith('Unnamed')]
        
        batch_tray = []
        
        for _, row in df.iterrows():
            raw_row_title = row['Metric']
            if pd.isna(raw_row_title) or str(raw_row_title).strip() == "":
                continue
                
            metric_slug = make_clean_slug(raw_row_title)
            
            for col in time_columns:
                raw_cell_value = str(row[col]).strip()
                clean_value = raw_cell_value.replace(',', '').replace('%', '').replace('"', '')
                
                if clean_value == '' or clean_value.lower() in ['nan', 'null', '-', '']:
                    continue
                    
                try:
                    numeric_value = float(clean_value)
                    standard_date = fix_date_format(col)
                    
                    batch_tray.append((
                        ticker,
                        statement_slug,
                        standard_date,
                        metric_slug,
                        str(raw_row_title).strip(),
                        numeric_value
                    ))
                except ValueError:
                    continue # Skips non-numeric textual lines safely
                    
        # 4. Insert all data items for this sheet vertically into Table 2
        if batch_tray:
            cursor.executemany("""
            INSERT INTO financial_records (ticker, statement_type, period_date, metric_slug, original_metric_name, financial_value)
            VALUES (?, ?, ?, ?, ?, ?);
            """, batch_tray)
            print(f"     Loaded {len(batch_tray)} cells into database.")

    # Save modifications and terminate session
    conn.commit()
    conn.close()

    # --- INVALIDATE AI VERDICT CACHE FOR THIS TICKER ---
    cache_file = "verdicts_cache.json"
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                cache = json.load(f)
            
            if ticker in cache:
                del cache[ticker]
                with open(cache_file, "w") as f:
                    json.dump(cache, f, indent=4)
                print(f"     [Cache] Cleared outdated AI verdict for {ticker}.")
        except Exception as e:
            print(f"     [Cache] Failed to invalidate cache: {e}")

if __name__ == "__main__":
    # Target your exact folder: 'excel_data'
    excel_workbooks = glob.glob("excel_data/*.xlsx")
    print(f"Found {len(excel_workbooks)} Excel workbooks inside 'excel_data/'. Starting data import...")
    
    for file_path in excel_workbooks:
        populate_single_stock(file_path)
        
    print("\n!!! SUCCESS !!! All files inside 'excel_data' folder are successfully unrolled into your database tables.")