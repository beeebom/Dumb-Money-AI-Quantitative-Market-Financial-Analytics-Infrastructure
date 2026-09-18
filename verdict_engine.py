import sqlite3
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).parent / ".env")

def get_financial_metric(cursor, ticker, metric_substring, statement_type=None, limit=1):
    """Helper to fetch the most recent data point for a metric."""
    query = "SELECT financial_value, metric_slug FROM financial_records WHERE ticker = ? AND metric_slug LIKE ?"
    params = [ticker, f"%{metric_substring}%"]
    
    if statement_type:
        query += " AND statement_type = ?"
        params.append(statement_type)
        
    query += " ORDER BY period_date DESC LIMIT ?"
    params.append(limit)
    
    cursor.execute(query, params)
    results = cursor.fetchall()
    
    if limit == 1:
        return results[0][0] if results else 0.0
    return [r[0] for r in results]

def calculate_business_score(cursor, ticker):
    # Pillar 1: Business Quality (Weight: 40%)
    roe = get_financial_metric(cursor, ticker, "roe_percentage")
    opm = get_financial_metric(cursor, ticker, "opm_percentage")
    sales_history = get_financial_metric(cursor, ticker, "sales", "profit_loss", limit=3)
    
    score = 0
    if roe > 15: score += 4
    elif roe > 10: score += 2
    
    if opm > 15: score += 3
    elif opm > 10: score += 1
    
    if len(sales_history) >= 2 and sales_history[0] > sales_history[1]:
        score += 3  # YoY Sales Growth
        
    return min(10, score)

def calculate_health_score(cursor, ticker):
    # Pillar 2: Financial Health (Weight: 25%)
    borrowings = get_financial_metric(cursor, ticker, "borrowings", "balance_sheet")
    equity = get_financial_metric(cursor, ticker, "equity_capital", "balance_sheet") + get_financial_metric(cursor, ticker, "reserves", "balance_sheet")
    
    debt_to_equity = (borrowings / equity) if equity > 0 else 0
    
    cfo = get_financial_metric(cursor, ticker, "cash_from_operating_activity")
    net_profit = get_financial_metric(cursor, ticker, "net_profit", "profit_loss")
    
    score = 0
    if debt_to_equity < 0.5: score += 5
    elif debt_to_equity < 1.0: score += 2
    
    if cfo > net_profit: score += 5
    elif cfo > 0: score += 2
    
    return min(10, score)

def calculate_valuation_score(cursor, ticker):
    # Pillar 3: Valuation / Earnings Trend (Weight: 20%)
    eps_history = get_financial_metric(cursor, ticker, "eps_in_rs", limit=3)
    
    score = 0
    if len(eps_history) >= 2 and eps_history[0] > eps_history[1]:
        score += 10
    elif len(eps_history) > 0 and eps_history[0] > 0:
        score += 5
        
    return score

def calculate_governance_score(cursor, ticker):
    # Pillar 4: Governance (Weight: 15%)
    # Sometimes saved as 'promoter_s_holding' depending on raw excel
    promoter_holding = get_financial_metric(cursor, ticker, "promoter")
    
    score = 0
    if promoter_holding > 50: score += 10
    elif promoter_holding > 30: score += 5
    
    return score

def get_ai_verdict(ticker, scores):
    # Retrieve Gemini API Key from environment
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

    if not GEMINI_API_KEY:
        return "GEMINI_API_KEY not set. Add it to your .env file to generate a text verdict."
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    You are an expert equity analyst. Write exactly TWO punchy sentences analyzing the stock '{ticker}' based on its Four Pillar scores out of 10:
    - Business Quality: {scores['business']}/10
    - Financial Health: {scores['health']}/10
    - Valuation/EPS Trend: {scores['valuation']}/10
    - Governance: {scores['governance']}/10
    
    Give a definitive opinion on whether it's fundamentally strong or weak. Keep it professional and direct. Do not include introductory text.
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.2)
        )
        return response.text.strip()
    except Exception as e:
        return f"Failed to connect to AI: {e}"

def generate_four_pillars_verdict(ticker):
    ticker = ticker.upper().strip()
    cache_file = "verdicts_cache.json"
    
    # 1. Check the local JSON cache first
    if os.path.exists(cache_file):
        with open(cache_file, "r") as f:
            try:
                cache = json.load(f)
                if ticker in cache:
                    print(f"\n[CACHE HIT] Instant Verdict for {ticker} (No AI cost incurred):")
                    print("-" * 50)
                    cached = cache[ticker]
                    for k, v in cached["scores"].items():
                        print(f"{k.capitalize()}: {v}/10")
                    print(f"\nFinal AI Verdict:\n{cached['justification']}")
                    print("-" * 50)
                    return
            except json.JSONDecodeError:
                cache = {}
    else:
        cache = {}

    # 2. Database missing cache - Calculate purely via Python math
    print(f"\n[CALCULATING] Computing pure mathematical scores for {ticker}...")
    conn = sqlite3.connect("financial_intelligence.db")
    cursor = conn.cursor()
    
    # Verify ticker exists
    cursor.execute("SELECT company_name FROM companies WHERE ticker = ?", (ticker,))
    if not cursor.fetchone():
        print(f"Error: No data found for {ticker} in the database. Run the scraper first.")
        conn.close()
        return

    scores = {
        "business": calculate_business_score(cursor, ticker),
        "health": calculate_health_score(cursor, ticker),
        "valuation": calculate_valuation_score(cursor, ticker),
        "governance": calculate_governance_score(cursor, ticker)
    }
    
    conn.close()
    
    # 3. Call AI once for the textual justification
    print(f"[AI GENERATION] Calling Gemini to generate verdict text for {ticker}...")
    ai_text = get_ai_verdict(ticker, scores)
    
    # 4. Save to cache
    cache[ticker] = {
        "scores": scores,
        "justification": ai_text
    }
    
    with open(cache_file, "w") as f:
        json.dump(cache, f, indent=4)
        
    print(f"\n[NEW VERDICT GENERATED AND CACHED]")
    print("-" * 50)
    for k, v in scores.items():
        print(f"{k.capitalize()}: {v}/10")
    print(f"\nFinal AI Verdict:\n{ai_text}")
    print("-" * 50)

if __name__ == "__main__":
    test_ticker = input("Enter ticker code to analyze (e.g., TCS): ")
    if test_ticker:
        generate_four_pillars_verdict(test_ticker)
#1. Business Quality (Max 10 Points)
#OE (Return on Equity): If it's above 15%, the stock gets +4 points. If it's just above 10%, it gets +2 points.
#PM (Operating Profit Margin): If it's above 15%, it gets +3 points. If it's just above 10%, it gets +1 point.
#Sales Growth: If the most recent year's sales are higher than the previous year (YoY growth), it gets +3 points.
#2. Financial Health (Max 10 Points)
#Debt to Equity Ratio: The script calculates this by dividing total Borrowings by total Equity (Share Capital + Reserves). If it is very low (under 0.5), it gets +5 points. If it's under 1.0, it gets +2 points.
#Cash vs Profit: If the actual Cash from Operating Activity is greater than the reported Net Profit, it gets +5 points (proving the profits are real cash). If cash is just positive (>0), it gets +2 points.
#3. Valuation (Max 10 Points)
#EPS Growth (Earnings Per Share): If the most recent EPS is higher than the previous year's EPS, it gets a full +10 points. If it didn't grow but is at least positive (the company is profitable), it gets +5 points.
#4. Governance (Max 10 Points)
#Promoter Holding: If the company promoters hold more than 50% of the shares, it gets a full +10 points. If they hold more than 30%, it gets +5 points.