import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).parent / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DB_PATH        = "financial_intelligence.db"
PROMPT_PATH    = "promt.txt"
MAX_RETRIES    = 2

FORBIDDEN_OPS  = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE"]


# ─── SETUP ────────────────────────────────────────────────────────────────────

def get_client():
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY not set. Add it to your .env file.")
    return genai.Client(api_key=GEMINI_API_KEY)


def get_system_prompt():
    if not os.path.exists(PROMPT_PATH):
        raise FileNotFoundError(f"'{PROMPT_PATH}' not found.")
    with open(PROMPT_PATH, "r") as f:
        return f.read()


# ─── STEP 1: GENERATE SQL ─────────────────────────────────────────────────────

def generate_sql(client, system_prompt, question, error_feedback=None):
    if error_feedback:
        content = f"""The previous SQL you generated failed with this error:
{error_feedback}

Original question: {question}

Fix the SQL and return only the corrected query."""
    else:
        content = question

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=content,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.0
        )
    )

    raw = response.text.strip()
    sql = raw.replace("```sqlite", "").replace("```sql", "").replace("```", "").strip()
    return sql


# ─── STEP 2: EXECUTE SQL (pure Python, no AI) ─────────────────────────────────

def validate_sql(sql):
    for op in FORBIDDEN_OPS:
        if op in sql.upper().split():
            raise ValueError(f"Blocked: '{op}' operation is not allowed.")


def execute_sql(sql):
    validate_sql(sql)

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        results = cursor.fetchall()
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        return columns, results
    finally:
        conn.close()


# ─── STEP 3: SYNTHESIZE ANSWER ────────────────────────────────────────────────

def synthesize_answer(client, question, columns, results):
    rows_text = "\n".join(
        "  " + " | ".join(f"{col}: {val}" for col, val in zip(columns, row))
        for row in results[:50]
    )

    prompt = f"""You are a financial analyst. A user asked:
"{question}"

The database returned this data:
{rows_text}

Write a clear, concise financial answer based strictly on this data.
Use numbers. Be factual. No hallucination."""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.0)
    )
    return response.text.strip()


# ─── GRACEFUL FALLBACK ────────────────────────────────────────────────────────

def suggest_rephrasing(client, question, last_sql, last_error):
    prompt = f"""A user asked this financial question:
"{question}"

This SQL was generated but failed:
{last_sql}

Error: {last_error}

In 2-3 sentences:
1. Briefly explain why this likely failed
2. Suggest how the user should rephrase the question to get better results
Keep it simple and helpful."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        return response.text.strip()
    except Exception:
        return "Could not generate a suggestion. Try rephrasing your question with specific stock names, years, or metric names."


def handle_empty_results(client, question, sql):
    prompt = f"""A user asked: "{question}"

The SQL ran successfully but returned zero rows:
{sql}

In 1-2 sentences, explain why there might be no data and suggest a rephrasing."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        )
        return response.text.strip()
    except Exception:
        return "No records found. Try checking the stock ticker, year, or metric name."


# ─── ORCHESTRATOR ─────────────────────────────────────────────────────────────

def ask_financial_agent(question):
    client        = get_client()
    system_prompt = get_system_prompt()

    print(f"\n[QUESTION]: {question}")

    sql        = None
    columns    = []
    results    = []
    last_error = None

    # ── Validate + Execute with retry ──
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sql = generate_sql(client, system_prompt, question, error_feedback=last_error)
            print(f"\n[SQL — attempt {attempt}]:\n{sql}\n")

            columns, results = execute_sql(sql)
            last_error = None
            break

        except ValueError as e:
            # Destructive SQL blocked — no retry needed
            print(f"\n[BLOCKED]: {e}")
            print("Please rephrase your question as a data retrieval query.")
            return

        except Exception as e:
            last_error = str(e)
            print(f"[ERROR on attempt {attempt}]: {last_error}")
            if attempt == MAX_RETRIES:
                print("\n[FAILED]: Could not generate a valid query after retries.")
                print("\n[SUGGESTION]:")
                print(suggest_rephrasing(client, question, sql, last_error))
                return

    # ── Handle empty results ──
    if not results:
        print("[DATA RETRIEVED]: No records found.")
        print("\n[SUGGESTION]:")
        print(handle_empty_results(client, question, sql))
        return

    # ── Print raw data ──
    print("[DATA RETRIEVED]:")
    for row in results[:20]:
        print("  ->", dict(zip(columns, row)))
    if len(results) > 20:
        print(f"  ... and {len(results) - 20} more rows")

    # ── Synthesize answer ──
    print("\n[ANSWER]:")
    print(synthesize_answer(client, question, columns, results))


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    while True:
        question = input("\nAsk a financial question (or 'exit'): ").strip()
        if question.lower() == "exit":
            break
        if question:
            ask_financial_agent(question)
