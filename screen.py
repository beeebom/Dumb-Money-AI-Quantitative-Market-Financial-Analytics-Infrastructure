
#python screen.py --refresh-all
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
import os
import json
import re
import time
import random
import pandas as pd
from populate_database import populate_single_stock


class ScreenerScraper:

    def __init__(self):
        self.base_url = "https://www.screener.in/company/"

    def scrape_company(self, company_code, consolidated=True):

        suffix = "consolidated/" if consolidated else ""
        url = f"{self.base_url}{company_code}/{suffix}"

        print(f"\nScraping: {url}")

        with sync_playwright() as p:

            browser = p.chromium.launch(
                headless=True
            )

            page = browser.new_page()

            # Helps reduce blocking by mimicking a modern real browser
            page.set_extra_http_headers({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8"
            })

            # Add a random delay before navigating to prevent rate-limiting during continuous loops
            sleep_time = random.uniform(2.5, 6.0)
            print(f"Waiting {sleep_time:.2f}s to prevent rate-limiting...")
            time.sleep(sleep_time)

            page.goto(
                url,
                timeout=60000
            )

            # wait for JS rendering
            page.wait_for_timeout(5000)

            html = page.content()

            browser.close()

        soup = BeautifulSoup(html, "html.parser")

        company_name, sector, industry = self.extract_profile(soup)

        data = {
            "company": company_code,
            "company_name": company_name,
            "sector": sector,
            "industry": industry,
            "quarterly_results": self.extract_table(
                soup,
                "quarters"
            ),
            "profit_loss": self.extract_table(
                soup,
                "profit-loss"
            ),
            "balance_sheet": self.extract_table(
                soup,
                "balance-sheet"
            ),
            "cash_flow": self.extract_table(
                soup,
                "cash-flow"
            ),
            "ratios_table": self.extract_table(
                soup,
                "ratios"
            ),
            # Section has two tables: [0] quarterly, [1] yearly. Take yearly to
            # match the '-03-31' annual convention used by the other statements.
            "shareholding": self.extract_table(
                soup,
                "shareholding",
                table_index=1
            ),
            "ratios": self.extract_ratios(soup)
        }

        return data

    def extract_profile(self, soup):
        """Pulls company name plus Screener's sector/industry classification.

        Screener links classification as /market/IN08/ (top-level sector) down to
        /market/IN08/IN0801/IN080101/ (industry); depth of the href is the level.
        """
        name = None
        h1 = soup.find("h1")
        if h1:
            name = h1.text.strip()

        sector = None
        industry = None

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("/market/IN"):
                continue

            label = a.get_text(strip=True)
            if not label:
                continue

            # '/market/IN08/' -> one code segment -> top-level sector
            segments = [s for s in href.split("/") if s]
            if len(segments) == 2 and sector is None:
                sector = label
            elif len(segments) > 2:
                # deepest link wins as the specific industry
                industry = label

        return name, sector, industry

    def extract_table(self, soup, section_id, table_index=0):

        try:

            section = soup.find(
                "section",
                {"id": section_id}
            )

            if not section:
                print(f"Section not found: {section_id}")
                return {}

            tables = section.find_all("table")

            if len(tables) <= table_index:
                print(f"No table found in: {section_id}")
                return {}

            table = tables[table_index]

            headers = []

            thead = table.find("thead")

            if thead:
                headers = [
                    th.text.strip()
                    for th in thead.find_all("th")
                ]
                # Fix first header if empty
                if headers and not headers[0]:
                    headers[0] = "Metric"

            rows = []

            tbody = table.find("tbody")

            if tbody:

                for tr in tbody.find_all("tr"):

                    cols = []
                    for td in tr.find_all(["td", "th"]):
                        # Clean text: remove +, replace non-breaking spaces, strip
                        text = td.text.strip().replace("\u00a0", " ").replace(" +", "").replace("+", "")
                        cols.append(text)

                    if cols:
                        rows.append(cols)

            return {
                "headers": headers,
                "rows": rows
            }

        except Exception as e:

            print(f"Error extracting {section_id}: {e}")

            return {}

    def extract_ratios(self, soup):

        ratios = {}

        try:
            # Target the top ratios section specifically
            top_ratios = soup.find("div", id="top-ratios")
            if not top_ratios:
                top_ratios = soup # fallback to global search if div not found

            ratio_items = top_ratios.find_all(
                "li",
                class_="flex flex-space-between"
            )

            for item in ratio_items:
                name_span = item.find("span", class_="name")
                value_span = item.find("span", class_="value")

                if name_span:
                    key = name_span.text.strip()
                    if value_span:
                        # Get full text including units and both values for High/Low
                        value = value_span.get_text(separator=" ", strip=True)
                    else:
                        # Fallback if structure is slightly different
                        all_spans = item.find_all("span")
                        if len(all_spans) >= 2:
                            value = " ".join([s.text.strip() for s in all_spans[1:]])
                        else:
                            value = item.text.replace(key, "").strip()
                    
                    ratios[key] = value

        except Exception as e:

            print(f"Ratio extraction error: {e}")

        return ratios


# ============================================
# SAVE JSON & TABLES
# ============================================

def save_and_display_data(data, company_code):
    # Create directories if they don't exist
    os.makedirs("json_data", exist_ok=True)
    os.makedirs("excel_data", exist_ok=True)

    # Save JSON
    json_filename = os.path.join("json_data", f"{company_code}.json")
    with open(json_filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    print(f"\nSaved JSON: {json_filename}")

    # Single Excel File
    excel_filename = os.path.join("excel_data", f"{company_code}.xlsx")
    
    with pd.ExcelWriter(excel_filename, engine="openpyxl") as writer:
        
        # Process Tables
        sections = ["quarterly_results", "profit_loss", "balance_sheet", "cash_flow", "ratios_table", "shareholding"]
        
        for section in sections:
            table_data = data.get(section)
            if table_data and table_data.get("headers") and table_data.get("rows"):
                sheet_name = section.replace("_", " ").title()[:31] # Excel sheet limit
                print(f"\n--- {sheet_name.upper()} ---")
                
                df = pd.DataFrame(table_data["rows"], columns=table_data["headers"])
                
                # Print to console
                print(df.to_string(index=False))
                
                # Save to Excel Sheet
                df.to_excel(writer, sheet_name=sheet_name, index=False)

        # Ratios
        if data.get("ratios"):
            ratios_df = pd.DataFrame(list(data["ratios"].items()), columns=["Ratio", "Value"])
            # Save to Excel before printing (safer)
            ratios_df.to_excel(writer, sheet_name="Ratios", index=False)
            
            print("\n--- RATIOS ---")
            try:
                print(ratios_df.to_string(index=False))
            except UnicodeEncodeError:
                # Fallback for consoles that don't support Unicode (like some Windows shells)
                clean_df = ratios_df.copy()
                clean_df["Value"] = clean_df["Value"].apply(lambda x: str(x).encode('ascii', 'ignore').decode('ascii'))
                clean_df["Ratio"] = clean_df["Ratio"].apply(lambda x: str(x).encode('ascii', 'ignore').decode('ascii'))
                print(clean_df.to_string(index=False))

    print(f"\nSuccessfully saved all tables to: {excel_filename}")


# ============================================
# MAIN
# ============================================

def get_all_tickers():
    from db import get_connection
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT ticker FROM companies ORDER BY ticker")
    tickers = [r[0] for r in cursor.fetchall()]
    cursor.close()
    conn.close()
    return tickers


def run_scraper(companies):
    scraper = ScreenerScraper()
    total   = len(companies)
    failed  = []

    for i, company in enumerate(companies, start=1):
        print(f"\n{'='*40}")
        print(f"[{i}/{total}] Processing: {company}")
        print(f"{'='*40}")
        try:
            data = scraper.scrape_company(company)
            save_and_display_data(data, company)

            excel_filepath = os.path.join("excel_data", f"{company}.xlsx")
            if os.path.exists(excel_filepath):
                populate_single_stock(excel_filepath)

            print(f"Done: {company}")

        except Exception as e:
            print(f"Error on {company}: {e}")
            failed.append(company)

    print(f"\n{'='*40}")
    print(f"Completed: {total - len(failed)}/{total} stocks")
    if failed:
        print(f"Failed: {', '.join(failed)}")
    print(f"{'='*40}")


if __name__ == "__main__":
    import sys

    if "--refresh-all" in sys.argv:
        print("Refresh All mode — loading all tickers from database...")
        companies = get_all_tickers()
        print(f"Found {len(companies)} stocks. Starting full quarterly refresh...\n")
        run_scraper(companies)

    else:
        companies_input = input(
            "\nEnter company codes separated by commas (example: TCS, INFY, RELIANCE): "
        ).strip().upper()

        if companies_input:
            companies = [c.strip() for c in companies_input.split(",") if c.strip()]
            run_scraper(companies)