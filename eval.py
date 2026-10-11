"""
Evaluation Framework for DumbMoney-RAG
----------------------------------------
Usage:
  python eval.py agent   -> evaluate Text-to-SQL agent
  python eval.py rag     -> evaluate RAG pipeline
  python eval.py both    -> evaluate both systems

Metrics tracked per run:
  - Latency       : total seconds, plus per-step breakdown
  - Cost          : estimated USD from token counts (Gemini 2.5 Flash pricing)
  - Quality       : LLM-as-judge scores (Relevance, Completeness, Accuracy) 1-5
  - SQL validity  : agent — did it use correct tables/columns?
  - Retrieval     : RAG — how many chunks, avg distance score
  - Pass/Fail     : binary execution success

Results appended to eval_results.json with timestamp.
"""

import sys
import json
import re
import time
from datetime import datetime
from pathlib import Path


# ─── GEMINI 2.5 FLASH PRICING (per million tokens, as of mid-2025) ────────────
# Input: $0.075 / 1M tokens  |  Output: $0.30 / 1M tokens
PRICE_INPUT_PER_M  = 0.075
PRICE_OUTPUT_PER_M = 0.30

def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens  / 1_000_000) * PRICE_INPUT_PER_M +
        (output_tokens / 1_000_000) * PRICE_OUTPUT_PER_M,
        6
    )

def count_tokens_approx(text: str) -> int:
    """~4 chars per token — good enough for cost estimates."""
    return max(1, len(text) // 4)


# ─── RATE-LIMIT HANDLING ──────────────────────────────────────────────────────
# The free tier allows 5 requests/min on gemini-2.5-flash and each question costs
# three calls (generate SQL + synthesize + judge). Without backoff, quota errors
# get recorded as agent failures and the scores become meaningless.

RETRYABLE = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE")


def call_with_retry(fn, *, max_attempts=5, base_delay=20):
    """Runs fn(), retrying transient quota/availability errors with backoff.

    Honors the server-provided retryDelay when present, otherwise backs off
    exponentially. Non-retryable errors are re-raised immediately.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            message = str(e)
            if not any(code in message for code in RETRYABLE):
                raise
            if attempt == max_attempts:
                raise

            delay = base_delay * (2 ** (attempt - 1))
            match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", message)
            if match:
                delay = float(match.group(1)) + 2

            print(f"    [rate-limit] attempt {attempt}/{max_attempts}, waiting {delay:.0f}s...")
            time.sleep(delay)


# ─── LLM-AS-JUDGE ─────────────────────────────────────────────────────────────

JUDGE_PROMPT = """You are an evaluation judge for a financial AI system.
Score the answer below on THREE dimensions. Reply with ONLY a JSON object, nothing else.

Question: {question}
Answer:   {answer}
Raw data hint (if any): {data_hint}

Score each dimension from 1 to 5:
- relevance   : Does the answer directly address the question? (1=off-topic, 5=perfectly on-point)
- completeness: Does it cover all key aspects the question asks for? (1=barely answers, 5=fully covers)
- accuracy    : Is the answer factually grounded in the data provided? (1=hallucinated, 5=fully grounded)

Respond ONLY with valid JSON like this:
{{"relevance": 4, "completeness": 3, "accuracy": 5, "reasoning": "one short sentence"}}"""


def judge_answer(client, question: str, answer: str, data_hint: str = "") -> dict:
    """Returns dict with relevance/completeness/accuracy scores (1-5) + reasoning."""
    if not answer or len(answer) < 10:
        return {"relevance": 1, "completeness": 1, "accuracy": 1, "reasoning": "No answer produced"}

    prompt = JUDGE_PROMPT.format(
        question=question,
        answer=answer[:800],
        data_hint=data_hint[:300] if data_hint else "N/A"
    )

    try:
        from google.genai import types
        response = call_with_retry(lambda: client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0)
        ))
        raw = response.text.strip()
        # strip markdown fences if present
        raw = raw.replace("```json", "").replace("```", "").strip()
        scores = json.loads(raw)
        return {
            "relevance"    : int(scores.get("relevance",    1)),
            "completeness" : int(scores.get("completeness", 1)),
            "accuracy"     : int(scores.get("accuracy",     1)),
            "reasoning"    : str(scores.get("reasoning",    ""))
        }
    except Exception as e:
        return {"relevance": 0, "completeness": 0, "accuracy": 0, "reasoning": f"Judge failed: {e}"}


# ─── TEST CASES ───────────────────────────────────────────────────────────────

AGENT_TESTS = [
    {
        "id": "A1",
        "question": "Show me top 5 stocks by net profit in 2024",
        "expected_tables": ["financial_records"],
        "expected_columns": ["net_profit", "ticker"]
    },
    {
        "id": "A2",
        "question": "Which sector had highest revenue growth from 2022 to 2024?",
        "expected_tables": ["financial_records", "companies"],
        "expected_columns": ["sector", "revenue", "sales"]
    },
    {
        "id": "A3",
        "question": "Show stocks where expenses grew faster than sales last year",
        "expected_tables": ["financial_records"],
        "expected_columns": ["expenses", "sales", "ticker"]
    },
    {
        "id": "A4",
        "question": "Which stocks have ROE above 15% for the latest year?",
        "expected_tables": ["financial_records"],
        "expected_columns": ["roe_percentage", "ticker"]
    },
    {
        "id": "A5",
        "question": "Show bottom 5 stocks by operating profit margin in 2023",
        "expected_tables": ["financial_records"],
        "expected_columns": ["opm_percentage", "ticker"]
    },
]

RAG_TESTS = [
    {"id": "R1", "question": "What dividend was declared?"},
    {"id": "R2", "question": "What are the key risks mentioned in the report?"},
    {"id": "R3", "question": "What did management say about future outlook?"},
    {"id": "R4", "question": "Were there any changes in capital allocation strategy?"},
    {"id": "R5", "question": "What were the major highlights of the board meeting?"},
]


# ─── SQL VALIDITY CHECK ───────────────────────────────────────────────────────

def check_sql_validity(sql: str, expected_tables: list, expected_columns: list) -> dict:
    """Checks if generated SQL references expected tables and at least one expected column."""
    sql_upper = sql.upper()
    tables_found  = [t for t in expected_tables  if t.upper() in sql_upper]
    columns_found = [c for c in expected_columns if c.upper() in sql_upper]
    score = round(
        (len(tables_found) / max(len(expected_tables), 1)) * 0.5 +
        (min(len(columns_found), 1) / 1)                  * 0.5,
        2
    )
    return {
        "score"          : score,        # 0.0 – 1.0
        "tables_found"   : tables_found,
        "columns_found"  : columns_found,
        "tables_missing" : [t for t in expected_tables  if t not in tables_found],
        "columns_missing": [c for c in expected_columns if c not in columns_found],
    }


# ─── AGENT EVALUATOR ──────────────────────────────────────────────────────────

def evaluate_agent(tests):
    from agent import get_client, get_system_prompt, generate_sql, execute_sql, synthesize_answer, MAX_RETRIES

    client        = get_client()
    system_prompt = get_system_prompt()
    results       = []

    print("\n" + "=" * 70)
    print("EVALUATING: Text-to-SQL Agent")
    print("=" * 70)

    for test in tests:
        print(f"\n[{test['id']}] {test['question']}")
        t_start    = time.time()
        status     = "fail"
        retries    = 0
        answer     = ""
        sql        = ""
        error_msg  = ""
        last_error = None
        columns    = []
        rows       = []

        total_input_tokens  = 0
        total_output_tokens = 0

        # Step timers
        t_sql_start = t_exec_start = t_synth_start = 0
        lat_sql = lat_exec = lat_synth = 0.0

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # Step 1: Generate SQL
                t_sql_start = time.time()
                sql = call_with_retry(lambda: generate_sql(
                    client, system_prompt, test["question"], error_feedback=last_error
                ))
                lat_sql = round(time.time() - t_sql_start, 2)

                total_input_tokens  += count_tokens_approx(system_prompt + test["question"])
                total_output_tokens += count_tokens_approx(sql)

                # Step 2: Execute
                t_exec_start = time.time()
                columns, rows = execute_sql(sql)
                lat_exec = round(time.time() - t_exec_start, 2)

                retries = attempt - 1

                if rows:
                    # Step 3: Synthesize
                    t_synth_start = time.time()
                    answer = call_with_retry(lambda: synthesize_answer(
                        client, test["question"], columns, rows
                    ))
                    lat_synth = round(time.time() - t_synth_start, 2)

                    total_input_tokens  += count_tokens_approx(test["question"] + str(rows[:50]))
                    total_output_tokens += count_tokens_approx(answer)

                    status = "pass"
                    print(f"  Execution : PASS ({len(rows)} rows)")
                else:
                    status    = "fail"
                    error_msg = "0 rows returned"
                    print(f"  Execution : FAIL (0 rows)")
                break

            except Exception as e:
                last_error = str(e)
                retries    = attempt
                lat_sql    = round(time.time() - t_sql_start, 2)
                if attempt == MAX_RETRIES:
                    status    = "fail"
                    error_msg = last_error
                    print(f"  Execution : FAIL ({error_msg[:80]})")

        total_latency = round(time.time() - t_start, 2)
        cost          = estimate_cost(total_input_tokens, total_output_tokens)

        # SQL validity check
        validity = check_sql_validity(
            sql,
            test.get("expected_tables",  []),
            test.get("expected_columns", [])
        )

        # Quality scores (LLM judge)
        data_hint = str([dict(zip(columns, r)) for r in rows[:5]]) if rows else ""
        quality   = judge_answer(client, test["question"], answer, data_hint)
        avg_score = round((quality["relevance"] + quality["completeness"] + quality["accuracy"]) / 3, 2)

        print(f"  Latency   : {total_latency}s  (sql={lat_sql}s | exec={lat_exec}s | synth={lat_synth}s)")
        print(f"  Cost      : ~${cost:.5f}  ({total_input_tokens} in / {total_output_tokens} out tokens)")
        print(f"  SQL valid : {validity['score']} | columns found: {validity['columns_found']}")
        print(f"  Quality   : relevance={quality['relevance']} completeness={quality['completeness']} accuracy={quality['accuracy']} avg={avg_score}")
        print(f"  Judge     : {quality['reasoning']}")

        # Free tier: 5 req/min on gemini-2.5-flash — pace requests
        if test != tests[-1]:
            time.sleep(15)

        results.append({
            "id"              : test["id"],
            "system"          : "agent",
            "question"        : test["question"],
            "status"          : status,
            "retries"         : retries,
            "latency_total_s" : total_latency,
            "latency_sql_s"   : lat_sql,
            "latency_exec_s"  : lat_exec,
            "latency_synth_s" : lat_synth,
            "tokens_input"    : total_input_tokens,
            "tokens_output"   : total_output_tokens,
            "cost_usd"        : cost,
            "sql_validity"    : validity["score"],
            "sql_columns_found": validity["columns_found"],
            "quality_relevance"    : quality["relevance"],
            "quality_completeness" : quality["completeness"],
            "quality_accuracy"     : quality["accuracy"],
            "quality_avg"          : avg_score,
            "judge_reasoning"      : quality["reasoning"],
            "answer_preview"       : answer[:300] if answer else "",
            "error"                : error_msg,
            "timestamp"            : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    return results


# ─── RAG EVALUATOR ────────────────────────────────────────────────────────────

def evaluate_rag(tests):
    import chromadb
    from sentence_transformers import SentenceTransformer
    from chunking import get_collection, build_prompt, EMBED_MODEL, TOP_K
    import os
    from dotenv import load_dotenv
    from google import genai as genai_new

    load_dotenv(Path(__file__).parent / ".env")
    api_key = os.getenv("GEMINI_API_KEY", "")

    judge_client = genai_new.Client(api_key=api_key)
    gemini       = judge_client  # reuse same client for generation

    model      = SentenceTransformer(EMBED_MODEL)
    collection = get_collection()
    results    = []

    print("\n" + "=" * 70)
    print("EVALUATING: RAG Pipeline")
    print("=" * 70)

    total = collection.count()
    if total == 0:
        print("Vector DB is empty. Run 'python chunking.py etl' first.")
        return []

    for test in tests:
        print(f"\n[{test['id']}] {test['question']}")
        t_start  = time.time()
        status   = "fail"
        answer   = ""
        error_msg= ""
        chunks_retrieved = 0
        avg_distance     = 0.0

        total_input_tokens  = 0
        total_output_tokens = 0

        t_embed_start = t_retrieve_start = t_gen_start = 0
        lat_embed = lat_retrieve = lat_gen = 0.0

        try:
            # Step 1: Embed query
            t_embed_start  = time.time()
            query_embedding = model.encode([test["question"]]).tolist()
            lat_embed       = round(time.time() - t_embed_start, 2)

            # Step 2: Retrieve chunks
            t_retrieve_start = time.time()
            res = collection.query(
                query_embeddings = query_embedding,
                n_results        = TOP_K,
                include          = ["documents", "metadatas", "distances"]
            )
            lat_retrieve = round(time.time() - t_retrieve_start, 2)

            chunks = [
                {"text": doc, "meta": meta}
                for doc, meta in zip(res["documents"][0], res["metadatas"][0])
            ]
            distances        = res["distances"][0] if res["distances"] else []
            chunks_retrieved = len(chunks)
            avg_distance     = round(sum(distances) / len(distances), 4) if distances else 0.0

            if chunks:
                # Step 3: Generate answer
                from google.genai import types as gentypes
                t_gen_start = time.time()
                prompt      = build_prompt(test["question"], chunks)
                response    = call_with_retry(lambda: gemini.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt,
                    config=gentypes.GenerateContentConfig(temperature=0.0)
                ))
                answer      = response.text.strip()
                lat_gen     = round(time.time() - t_gen_start, 2)

                total_input_tokens  += count_tokens_approx(prompt)
                total_output_tokens += count_tokens_approx(answer)

                if "could not find" in answer.lower() or len(answer) < 20:
                    status    = "fail"
                    error_msg = "Model could not find answer in retrieved chunks"
                    print(f"  Execution : FAIL (no answer in chunks)")
                else:
                    status = "pass"
                    print(f"  Execution : PASS ({chunks_retrieved} chunks | avg_dist={avg_distance})")
            else:
                error_msg = "No chunks retrieved"
                print(f"  Execution : FAIL (no chunks retrieved)")

        except Exception as e:
            error_msg = str(e)
            print(f"  Execution : FAIL ({error_msg[:80]})")

        total_latency = round(time.time() - t_start, 2)
        cost          = estimate_cost(total_input_tokens, total_output_tokens)

        # Quality scores (LLM judge)
        quality   = judge_answer(judge_client, test["question"], answer)
        avg_score = round((quality["relevance"] + quality["completeness"] + quality["accuracy"]) / 3, 2)

        print(f"  Latency   : {total_latency}s  (embed={lat_embed}s | retrieve={lat_retrieve}s | gen={lat_gen}s)")
        print(f"  Cost      : ~${cost:.5f}  ({total_input_tokens} in / {total_output_tokens} out tokens)")
        print(f"  Retrieval : {chunks_retrieved} chunks | avg_distance={avg_distance} (lower=better)")
        print(f"  Quality   : relevance={quality['relevance']} completeness={quality['completeness']} accuracy={quality['accuracy']} avg={avg_score}")
        print(f"  Judge     : {quality['reasoning']}")

        # Free tier: 5 req/min on gemini-2.5-flash — pace requests
        if test != tests[-1]:
            time.sleep(15)

        results.append({
            "id"               : test["id"],
            "system"           : "rag",
            "question"         : test["question"],
            "status"           : status,
            "chunks_retrieved" : chunks_retrieved,
            "avg_distance"     : avg_distance,
            "latency_total_s"  : total_latency,
            "latency_embed_s"  : lat_embed,
            "latency_retrieve_s": lat_retrieve,
            "latency_gen_s"    : lat_gen,
            "tokens_input"     : total_input_tokens,
            "tokens_output"    : total_output_tokens,
            "cost_usd"         : cost,
            "quality_relevance"    : quality["relevance"],
            "quality_completeness" : quality["completeness"],
            "quality_accuracy"     : quality["accuracy"],
            "quality_avg"          : avg_score,
            "judge_reasoning"      : quality["reasoning"],
            "answer_preview"       : answer[:300] if answer else "",
            "error"                : error_msg,
            "timestamp"            : datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    return results


# ─── SAVE + PRINT RESULTS ─────────────────────────────────────────────────────

def save_results(new_results):
    output_file = Path("eval_results.json")
    all_results = []

    if output_file.exists():
        with open(output_file, "r", encoding="utf-8") as f:
            all_results = json.load(f)

    all_results.extend(new_results)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    return output_file


def print_summary(results):
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    passed   = sum(1 for r in results if r["status"] == "pass")
    failed   = sum(1 for r in results if r["status"] == "fail")
    total_cost = round(sum(r.get("cost_usd", 0) for r in results), 6)
    avg_lat    = round(sum(r["latency_total_s"] for r in results) / len(results), 2)
    avg_qual   = round(sum(r.get("quality_avg", 0) for r in results) / len(results), 2)

    print("\n" + "=" * 90)
    print(f"EVALUATION SUMMARY — {run_time}")
    print("=" * 90)
    print(f"{'ID':<5} {'Sys':<7} {'Status':<7} {'Lat(s)':<8} {'Cost($)':<10} {'R':<4} {'C':<4} {'A':<4} {'Avg':<5} Question")
    print("-" * 90)

    for r in results:
        rel  = r.get("quality_relevance",    "-")
        comp = r.get("quality_completeness", "-")
        acc  = r.get("quality_accuracy",     "-")
        avg  = r.get("quality_avg",          "-")
        cost = f"{r.get('cost_usd', 0):.5f}"
        print(
            f"{r['id']:<5} {r['system']:<7} {r['status'].upper():<7} "
            f"{r['latency_total_s']:<8} {cost:<10} "
            f"{rel:<4} {comp:<4} {acc:<4} {avg:<5} "
            f"{r['question'][:40]}..."
        )

    print("-" * 90)
    print(f"Pass: {passed}/{len(results)} | Fail: {failed}/{len(results)} | Avg Latency: {avg_lat}s | Total Cost: ~${total_cost} | Avg Quality: {avg_qual}/5")
    print("(R=Relevance  C=Completeness  A=Accuracy — all scored 1-5 by Gemini judge)")
    print("=" * 90)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    mode = args[0] if args else "both"

    if mode not in ("agent", "rag", "both"):
        print("Usage: python eval.py agent | rag | both")
        sys.exit(1)

    all_results = []

    if mode in ("agent", "both"):
        all_results += evaluate_agent(AGENT_TESTS)

    if mode in ("rag", "both"):
        all_results += evaluate_rag(RAG_TESTS)

    output_file = save_results(all_results)
    print_summary(all_results)
    print(f"\nResults appended to: {output_file}")
