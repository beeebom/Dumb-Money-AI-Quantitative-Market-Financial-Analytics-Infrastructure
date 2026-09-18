# DumbMoney — Quantitative Market & Financial Analytics Infrastructure

A data + LLM infrastructure project that turns unstructured Indian equity filings (Screener.in) into a **queryable financial database**, exposed through a **Text-to-SQL agent**, a **document RAG pipeline**, and a **deterministic scoring engine** — with a built-in evaluation harness to measure all of it.

The core design principle: **LLMs translate and explain, they never compute.** Every number a user sees is produced by SQL or Python arithmetic against verified data; Gemini's only jobs are (1) turning English into SQL and (2) turning rows into prose.

```
┌─────────────┐     ┌──────────────┐     ┌───────────────────┐     ┌─────────────┐
│  screen.py  │ --> │ populate_    │ --> │ financial_         │ --> │  agent.py   │
│ (Playwright │     │ database.py  │     │ intelligence.db    │     │ (Text-to-   │
│  scraper)   │     │ (ETL/unpivot)│     │ (SQLite, vertical  │     │  SQL agent) │
└─────────────┘     └──────────────┘     │  schema)           │     └─────────────┘
                                          └─────────┬──────────┘
                                                     │
                                          ┌──────────┴──────────┐
                                          │  verdict_engine.py  │
                                          │ (Four Pillars score)│
                                          └─────────────────────┘

┌──────────────┐     ┌─────────────┐     ┌─────────────┐
│ input_pdfs/  │ --> │ chunking.py │ --> │  ChromaDB   │  (RAG over quarterly report PDFs)
└──────────────┘     └─────────────┘     └─────────────┘

                      ┌─────────┐
                      │ eval.py │  — latency / cost / LLM-judge quality harness for both systems
                      └─────────┘
```

---

## What it does

**1. Scrapes real financial statements at scale.**
[`screen.py`](screen.py) drives a headless Playwright browser against Screener.in, pulling quarterly results, P&L, balance sheet, cash flow, and ratio tables per ticker, with randomized delays and header spoofing to stay polite under sustained crawling. `--refresh-all` re-scrapes every ticker already in the database.

**2. Normalizes messy spreadsheets into a queryable schema.**
[`populate_database.py`](populate_database.py) takes each scraped workbook — wide, human-formatted, inconsistent (`OPM %`, `Mar 2026`, stray commas) — and unpivots it into a clean **long-format relational schema**: one row per `(ticker, statement_type, period_date, metric_slug, value)`. Row titles are slugified into a fixed vocabulary (`opm_percentage`, `roe_percentage`, `cash_from_operating_activity`, …) so downstream SQL never has to fuzzy-match text.

**3. Answers financial questions in plain English.**
[`agent.py`](agent.py) is a three-stage Text-to-SQL agent:
- **Generate** — Gemini 2.5 Flash writes SQLite given a schema-and-slug-constrained system prompt ([`promt.txt`](promt.txt)), temperature `0`.
- **Validate & execute** — pure Python, zero LLM involvement. A hard blocklist (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `ALTER`, `TRUNCATE`) rejects destructive SQL before it ever touches the database; the query then runs directly against SQLite.
- **Synthesize** — a second Gemini call turns the raw result rows into a grounded, numbers-first answer.

Self-correcting on failure: a failed query's error message is fed back into a second generation attempt (`MAX_RETRIES = 2`); if it still fails, or returns zero rows, the agent asks Gemini to explain *why* in plain language and suggest a better phrasing — instead of silently returning nothing.

**4. Scores every stock with deterministic math, not vibes.**
[`verdict_engine.py`](verdict_engine.py) computes a **Four Pillars** score (0–10 each) purely in Python:

| Pillar | Weight | Signal |
|---|---|---|
| Business Quality | 40% | ROE, operating margin, YoY sales growth |
| Financial Health | 25% | Debt/equity, cash-flow-vs-reported-profit sanity check |
| Valuation / Earnings Trend | 20% | YoY EPS growth |
| Governance | 15% | Promoter shareholding |

Gemini is called **once per ticker**, only to phrase a two-sentence verdict from the already-computed scores — never to decide the scores themselves. Results are cached to `verdicts_cache.json` and automatically invalidated whenever fresh data lands for that ticker.

**5. Also answers questions from unstructured PDFs (RAG).**
[`chunking.py`](chunking.py) parses quarterly report PDFs with PyMuPDF, splits on detected section headings (with a fixed-size fallback, 500 chars / 50 overlap), embeds chunks with `BAAI/bge-small-en-v1.5`, and serves top-k retrieval + generation through ChromaDB. This path is intentionally the newer, less mature half of the system — see [Evaluation](#evaluation) below for exactly where it currently falls short.

**6. Measures itself.**
[`eval.py`](eval.py) is a harness that runs a fixed question set against both the SQL agent and the RAG pipeline and scores every run on:
- **Latency** — end-to-end and per-stage (generate / execute / synthesize, or embed / retrieve / generate)
- **Cost** — estimated USD from actual token counts at Gemini 2.5 Flash pricing
- **Quality** — an LLM-as-judge rubric (relevance, completeness, accuracy, 1–5 each)
- **Correctness** — SQL validity / expected-column presence for the agent, retrieval distance for RAG
- **Pass/fail** — binary execution success

Every run appends timestamped results to [`eval_results.json`](eval_results.json), so quality and cost regressions are visible over time instead of anecdotal.

---

## Data schema

Two tables, vertical by design — new metrics or statement types need no migration:

```sql
CREATE TABLE companies (
    ticker          TEXT PRIMARY KEY,
    company_name    TEXT,
    sector          TEXT,
    index_category  TEXT
);

CREATE TABLE financial_records (
    record_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT REFERENCES companies(ticker),
    statement_type        TEXT,   -- quarterly_results | profit_loss | balance_sheet | cash_flow | ratios
    period_date            TEXT,   -- 'YYYY-MM-DD'; annual periods end '-03-31'
    metric_slug            TEXT,   -- fixed vocabulary, e.g. 'roe_percentage', 'net_profit'
    original_metric_name  TEXT,   -- source text, preserved for auditability
    financial_value        REAL
);
```

The full constrained slug vocabulary (what the LLM is allowed to reference) lives in [`promt.txt`](promt.txt) — this is what keeps generated SQL from hallucinating column or metric names.

**Current dataset:** 171 NSE-listed companies, **85,600+ data points**, spanning FY2006 to TTM.

---

## Evaluation

Run with `python eval.py agent | rag | both`. This is a real, checked-in harness — numbers below are from the last recorded run in [`eval_results.json`](eval_results.json), not aspirational claims.

| System | Pass rate | Avg latency | Avg judge score |
|---|---|---|---|
| Text-to-SQL Agent | 2/5 | ~7.0s | 1.6 / 5 |
| RAG (PDF) | 0/5 | ~0.4s | 1.0 / 5 |

Honest state of things: the recorded RAG run failed end-to-end on a stale `gemini-1.5-flash` model reference (visible directly in the logged `error` field) — the retrieval path works, generation needs its model id updated. The agent's low pass rate on that particular batch reflects harder multi-step questions in the test set (YoY comparisons, cross-sector joins) rather than basic query failures. The harness exists specifically to surface exactly this kind of gap instead of hiding it — that's the point of building it.

---

## Project layout

```
agent.py               Text-to-SQL financial agent (generate → validate → execute → synthesize)
verdict_engine.py      Four Pillars deterministic scoring engine + cached AI verdicts
chunking.py             PDF → chunks → embeddings → ChromaDB (RAG pipeline)
eval.py                 Latency/cost/quality evaluation harness for both systems
screen.py               Playwright scraper for Screener.in (per-ticker, --refresh-all)
populate_database.py   Excel → normalized SQLite ETL
download_pdfs.py       Best-effort web search + download of quarterly report PDFs
db.py                   Schema definition (companies, financial_records)
promt.txt               System prompt + constrained schema/slug vocabulary for the SQL agent
financial_intelligence.db   SQLite database (171 tickers, 85k+ records, included for exploration)
excel_data/, json_data/     Raw scraped snapshots per ticker
eval_results.json       Logged evaluation runs
verdicts_cache.json     Cached Four Pillars verdicts
```

---

## Setup

```bash
git clone https://github.com/beeebom/Dumb-Money-AI-Quantitative-Market-Financial-Analytics-Infrastructure.git
cd Dumb-Money-AI-Quantitative-Market-Financial-Analytics-Infrastructure

python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate # macOS/Linux

pip install -r requirements.txt
playwright install chromium

echo GEMINI_API_KEY=your_key_here > .env
```

`financial_intelligence.db` ships pre-populated, so the agent and verdict engine work immediately:

```bash
python agent.py            # interactive Text-to-SQL financial Q&A
python verdict_engine.py   # Four Pillars score + AI verdict for a ticker
python eval.py both        # run the evaluation harness
python screen.py --refresh-all   # re-scrape and refresh all 171 tickers
```

Get a free Gemini API key at [aistudio.google.com](https://aistudio.google.com/apikey).

---

## Design notes

- **No LLM ever touches raw arithmetic.** Scores, ratios, and growth calculations are computed in Python; Gemini only translates language ↔ structured data and phrases the result. This bounds hallucination risk to *interpretation*, not *fact generation*.
- **SQL execution is sandboxed by an operation blocklist**, independent of the model — a prompt injection that convinces the LLM to emit `DROP TABLE` still gets rejected in `execute_sql()` before it reaches SQLite.
- **Every failure path talks back.** A bad query, an empty result set, and a bad API call each get their own user-facing explanation instead of a stack trace or silent `None`.
- **The vertical schema is metric-agnostic.** Adding a new financial ratio or a new statement type requires zero schema migration — just a new `metric_slug` value and an update to the constrained vocabulary in `promt.txt`.

---

## License

MIT — see [LICENSE](LICENSE).
