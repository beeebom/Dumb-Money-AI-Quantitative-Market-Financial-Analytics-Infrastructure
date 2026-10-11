"""Backfills companies.sector / index_category from Screener.

Full re-scrapes are slow because they render every financial table; sector only
needs the page header, so this fetches classification alone for tickers that are
still missing it.
"""

import json
import os
import random
import time

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from db import ensure_schema, get_connection
from screen import ScreenerScraper


def tickers_missing_sector():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ticker FROM companies
        WHERE sector IS NULL OR sector = 'Unassigned Sector'
        ORDER BY ticker
    """)
    tickers = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    return tickers


def update_profile(ticker, name, sector, industry):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO companies (ticker, company_name, sector, index_category)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ticker) DO UPDATE SET
            company_name   = COALESCE(EXCLUDED.company_name, companies.company_name),
            sector         = COALESCE(EXCLUDED.sector, companies.sector),
            index_category = COALESCE(EXCLUDED.index_category, companies.index_category);
    """, (ticker, name, sector, industry))
    conn.commit()
    cursor.close()
    conn.close()


def merge_into_json(ticker, name, sector, industry):
    """Keeps the scraper's JSON sidecar in sync so re-populates stay correct."""
    path = os.path.join("json_data", f"{ticker}.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    data["company_name"] = name
    data["sector"] = sector
    data["industry"] = industry

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def run():
    ensure_schema()
    tickers = tickers_missing_sector()
    print(f"{len(tickers)} tickers missing sector. Starting backfill...\n")

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
                page.wait_for_timeout(1500)

                soup = BeautifulSoup(page.content(), "html.parser")
                name, sector, industry = scraper.extract_profile(soup)

                if not sector:
                    print(f"[{i}/{len(tickers)}] {ticker}: no sector found")
                    failed.append(ticker)
                    continue

                update_profile(ticker, name, sector, industry)
                merge_into_json(ticker, name, sector, industry)
                print(f"[{i}/{len(tickers)}] {ticker}: {sector} / {industry}")

            except Exception as e:
                print(f"[{i}/{len(tickers)}] {ticker}: ERROR {e}")
                failed.append(ticker)

        browser.close()

    print(f"\nDone. {len(tickers) - len(failed)}/{len(tickers)} backfilled.")
    if failed:
        print(f"Failed: {', '.join(failed)}")


if __name__ == "__main__":
    run()
