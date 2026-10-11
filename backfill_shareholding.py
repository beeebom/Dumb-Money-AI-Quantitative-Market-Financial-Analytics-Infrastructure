"""Backfills the shareholding statement (promoter holding etc.) from Screener.

The scraper originally skipped the #shareholding section, so verdict_engine's
Governance pillar scored 0/10 for every ticker. This fetches just that section
for tickers missing it, rather than re-running a full financial re-scrape.
"""

import random
import time

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from db import ensure_schema, get_connection
from populate_database import fix_date_format, make_clean_slug
from screen import ScreenerScraper


def tickers_missing_shareholding():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.ticker
        FROM companies c
        WHERE NOT EXISTS (
            SELECT 1 FROM financial_records r
            WHERE r.ticker = c.ticker AND r.statement_type = 'shareholding'
        )
        ORDER BY c.ticker
    """)
    tickers = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    return tickers


def save_shareholding(ticker, table):
    """Unpivots the shareholding table into financial_records."""
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if not headers or not rows:
        return 0

    time_columns = [(i, h) for i, h in enumerate(headers) if i > 0 and h]

    batch = []
    for row in rows:
        if not row:
            continue
        raw_title = row[0].strip()
        if not raw_title:
            continue
        slug = make_clean_slug(raw_title)

        for idx, header in time_columns:
            if idx >= len(row):
                continue
            cleaned = row[idx].strip().replace(",", "").replace("%", "")
            if not cleaned or cleaned.lower() in ("nan", "null", "-"):
                continue
            try:
                value = float(cleaned)
            except ValueError:
                continue
            batch.append((
                ticker,
                "shareholding",
                fix_date_format(header),
                slug,
                raw_title,
                value,
            ))

    if not batch:
        return 0

    conn = get_connection()
    cursor = conn.cursor()
    cursor.executemany("""
        INSERT INTO financial_records (ticker, statement_type, period_date, metric_slug, original_metric_name, financial_value)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (ticker, statement_type, period_date, metric_slug)
        DO UPDATE SET
            original_metric_name = EXCLUDED.original_metric_name,
            financial_value = EXCLUDED.financial_value;
    """, batch)
    conn.commit()
    cursor.close()
    conn.close()
    return len(batch)


def run():
    ensure_schema()
    tickers = tickers_missing_shareholding()
    print(f"{len(tickers)} tickers missing shareholding data. Starting backfill...\n")

    scraper = ScreenerScraper()
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        })

        for i, ticker in enumerate(tickers, start=1):
            try:
                time.sleep(random.uniform(1.5, 3.5))
                page.goto(
                    f"https://www.screener.in/company/{ticker}/consolidated/",
                    timeout=60000,
                )
                page.wait_for_timeout(2000)

                soup = BeautifulSoup(page.content(), "html.parser")
                table = scraper.extract_table(soup, "shareholding", table_index=1)
                cells = save_shareholding(ticker, table)

                if not cells:
                    print(f"[{i}/{len(tickers)}] {ticker}: no shareholding data")
                    failed.append(ticker)
                    continue

                print(f"[{i}/{len(tickers)}] {ticker}: {cells} cells")

            except Exception as e:
                print(f"[{i}/{len(tickers)}] {ticker}: ERROR {e}")
                failed.append(ticker)

        browser.close()

    print(f"\nDone. {len(tickers) - len(failed)}/{len(tickers)} backfilled.")
    if failed:
        print(f"Failed: {', '.join(failed)}")


if __name__ == "__main__":
    run()
