"""

Folder convention:
  input_pdfs/
    hdfc/
      hdfc_q1_2024.pdf
    sbi/
      sbi_q1_2024.pdf
"""

import re
import json
import sys
import os
import fitz
import chromadb
import google.generativeai as genai
from pathlib import Path
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv(Path(__file__).parent / ".env")


# ─── CONFIG ───────────────────────────────────────────────────────────────────

CHUNK_SIZE    = 500
CHUNK_OVERLAP = 50
INPUT_FOLDER  = "input_pdfs"
OUTPUT_FOLDER = "data/output/chunks"
CHROMA_FOLDER = "data/output/vector_db"
COLLECTION    = "quarterly_reports"
EMBED_MODEL   = "BAAI/bge-small-en-v1.5"
GEMINI_MODEL  = "gemini-2.5-flash"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
TOP_K         = 5

HEADING_PATTERN = re.compile(
    r"^(?:[A-Z][A-Z\s\d\-&,]{3,}|"
    r"(?:PART|ITEM|SECTION|NOTE)\s+\d+[A-Z]?\.?\s*.+)$",
    re.MULTILINE
)

NOISE_PATTERN = re.compile(
    r"^(\d+\s*$"                      # lone page numbers
    r"|page\s+\d+\s*(of\s+\d+)?$"    # "page 1 of 10"
    r"|^\s*-+\s*page\s*-+\s*$"       # "--- page ---"
    r")",
    re.IGNORECASE
)


# ─── EXTRACT ──────────────────────────────────────────────────────────────────

def parse_filename(filename: str) -> dict:
    """Parse stock, quarter, year from filename like hdfc_q1_2024.pdf"""
    stem = Path(filename).stem.lower()
    parts = stem.split("_")

    stock   = parts[0] if len(parts) > 0 else "unknown"
    quarter = next((p.upper() for p in parts if re.match(r"q[1-4]", p, re.I)), "unknown")
    year    = next((p for p in parts if re.match(r"20\d{2}", p)), "unknown")

    return {"stock": stock, "quarter": quarter, "year": year}


def extract_text(pdf_path: str) -> str:
    full_text = []

    doc = fitz.open(pdf_path)
    for page_num, page in enumerate(doc, start=1):
        text = page.get_text()
        if text and text.strip():
            full_text.append(f"\n--- Page {page_num} ---\n{text.strip()}")
    doc.close()

    return "\n".join(full_text)


# ─── TRANSFORM ────────────────────────────────────────────────────────────────

def clean_line(line: str) -> str:
    line = line.strip()
    if NOISE_PATTERN.match(line):
        return ""
    return line


def split_into_sections(text: str) -> list[dict]:
    lines = text.split("\n")
    sections = []
    current_heading = "Introduction"
    current_lines = []

    for line in lines:
        cleaned = clean_line(line)
        if not cleaned:
            continue

        if HEADING_PATTERN.match(cleaned) and len(cleaned) < 120:
            if current_lines:
                sections.append({
                    "heading": current_heading,
                    "text": " ".join(current_lines).strip()
                })
            current_heading = cleaned
            current_lines = []
        else:
            current_lines.append(cleaned)

    if current_lines:
        sections.append({
            "heading": current_heading,
            "text": " ".join(current_lines).strip()
        })

    return sections


def chunk_text(text: str) -> list[str]:
    words = text.split()
    chunks = []
    start = 0

    while start < len(words):
        end = min(start + CHUNK_SIZE, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += CHUNK_SIZE - CHUNK_OVERLAP

    return chunks


def build_chunks(sections: list[dict], pdf_path: Path) -> list[dict]:
    meta     = parse_filename(pdf_path.name)
    stem     = pdf_path.stem
    all_chunks = []
    chunk_index = 0

    for section in sections:
        text = section["text"]
        if not text.strip():
            continue

        word_count = len(text.split())
        base = {
            "source_file" : pdf_path.name,
            "stock"       : meta["stock"],
            "quarter"     : meta["quarter"],
            "year"        : meta["year"],
            "heading"     : section["heading"],
        }

        if word_count <= CHUNK_SIZE:
            all_chunks.append({
                "chunk_id"  : f"{stem}_{chunk_index}",
                **base,
                "text"      : text,
                "word_count": word_count
            })
            chunk_index += 1
        else:
            for i, sub in enumerate(chunk_text(text)):
                all_chunks.append({
                    "chunk_id"  : f"{stem}_{chunk_index}",
                    **base,
                    "sub_chunk" : i + 1,
                    "text"      : sub,
                    "word_count": len(sub.split())
                })
                chunk_index += 1

    return all_chunks


# ─── LOAD (JSON) ──────────────────────────────────────────────────────────────

def save_chunks(chunks: list[dict], output_dir: Path, pdf_path: Path):
    stock_dir = output_dir / parse_filename(pdf_path.name)["stock"]
    stock_dir.mkdir(parents=True, exist_ok=True)

    out_file = stock_dir / f"{pdf_path.stem}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)

    return out_file


# ─── LOAD (VECTOR DB) ─────────────────────────────────────────────────────────

def get_collection():
    client = chromadb.PersistentClient(path=CHROMA_FOLDER)
    return client.get_or_create_collection(name=COLLECTION)


def embed_chunks(chunks: list[dict], model: SentenceTransformer):
    collection = get_collection()

    existing = set(collection.get()["ids"])
    new_chunks = [c for c in chunks if c["chunk_id"] not in existing]

    if not new_chunks:
        return 0

    texts      = [c["text"] for c in new_chunks]
    embeddings = model.encode(texts, show_progress_bar=True).tolist()

    collection.add(
        ids        = [c["chunk_id"] for c in new_chunks],
        embeddings = embeddings,
        documents  = texts,
        metadatas  = [
            {
                "source_file": c["source_file"],
                "stock"      : c["stock"],
                "quarter"    : c["quarter"],
                "year"       : c["year"],
                "heading"    : c["heading"],
            }
            for c in new_chunks
        ]
    )

    return len(new_chunks)


# ─── ORCHESTRATOR ─────────────────────────────────────────────────────────────

def run_etl(reindex: bool = False):
    input_dir  = Path(INPUT_FOLDER)
    output_dir = Path(OUTPUT_FOLDER)

    if not input_dir.exists():
        input_dir.mkdir(parents=True)
        print(f"Created '{INPUT_FOLDER}/' — add stock subfolders with PDFs and run again.")
        return

    output_dir.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(input_dir.rglob("*.pdf"))
    if not pdf_files:
        print(f"No PDFs found in '{INPUT_FOLDER}/'")
        return

    print(f"Found {len(pdf_files)} PDF(s)\n")
    print("Loading embedding model...")
    model = SentenceTransformer(EMBED_MODEL)

    new_count = skipped_count = 0

    for pdf_path in pdf_files:
        meta         = parse_filename(pdf_path.name)
        stock_dir    = output_dir / meta["stock"]
        json_output  = stock_dir / f"{pdf_path.stem}.json"

        if json_output.exists() and not reindex:
            print(f"  Skipping (already processed): {pdf_path.name}")
            skipped_count += 1
            continue

        print(f"\n  [{meta['stock'].upper()}] {pdf_path.name}")

        # Extract
        raw_text = extract_text(str(pdf_path))
        print(f"    Extracted {len(raw_text.split())} words")

        # Transform
        sections = split_into_sections(raw_text)
        print(f"    Found {len(sections)} sections")

        chunks = build_chunks(sections, pdf_path)
        print(f"    Generated {len(chunks)} chunks")

        # Load → JSON
        out_file = save_chunks(chunks, output_dir, pdf_path)
        print(f"    Saved JSON: {out_file}")

        # Load → Vector DB
        embedded = embed_chunks(chunks, model)
        print(f"    Embedded {embedded} chunks into vector DB")

        new_count += 1

    print(f"\nDone. Processed {new_count} new PDF(s), skipped {skipped_count}.")


# ─── QUERY ────────────────────────────────────────────────────────────────────

def run_query():
    print("Loading embedding model...")
    model      = SentenceTransformer(EMBED_MODEL)
    collection = get_collection()

    total = collection.count()
    if total == 0:
        print("Vector DB is empty. Run 'python chunking.py etl' first.")
        return

    print(f"Vector DB ready — {total} chunks indexed.")
    print("Filters are optional. Press Enter to skip.\n")

    while True:
        query = input("Query (or 'exit'): ").strip()
        if query.lower() == "exit":
            break
        if not query:
            continue

        stock   = input("  Filter by stock (e.g. hdfc): ").strip().lower() or None
        quarter = input("  Filter by quarter (e.g. Q1): ").strip().upper() or None
        year    = input("  Filter by year (e.g. 2024): ").strip() or None

        conditions = []
        if stock:
            conditions.append({"stock": {"$eq": stock}})
        if quarter:
            conditions.append({"quarter": {"$eq": quarter}})
        if year:
            conditions.append({"year": {"$eq": year}})

        if len(conditions) == 0:
            where = None
        elif len(conditions) == 1:
            where = conditions[0]
        else:
            where = {"$and": conditions}

        query_embedding = model.encode([query]).tolist()

        results = collection.query(
            query_embeddings = query_embedding,
            n_results        = TOP_K,
            where            = where,
            include          = ["documents", "metadatas", "distances"]
        )

        print(f"\nTop {TOP_K} results:\n")
        for i, (doc, meta, dist) in enumerate(zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0]
        )):
            print(f"  [{i+1}] {meta['stock'].upper()} | {meta['quarter']} {meta['year']} | {meta['heading']}")
            print(f"       Score: {round(1 - dist, 4)}")
            print(f"       {doc[:300]}...")
            print()


# ─── RAG ──────────────────────────────────────────────────────────────────────

def build_prompt(query: str, chunks: list[dict]) -> str:
    context_blocks = []
    for i, c in enumerate(chunks, start=1):
        meta = c["meta"]
        context_blocks.append(
            f"[{i}] {meta['stock'].upper()} | {meta['quarter']} {meta['year']} | {meta['heading']}\n{c['text']}"
        )

    context = "\n\n".join(context_blocks)

    return f"""You are a financial analyst assistant. Answer the user's question using ONLY the context below from quarterly reports.
If the answer is not in the context, say "I could not find this information in the provided reports."
Be concise and factual. Cite the source (stock, quarter, year) for each point you make.

CONTEXT:
{context}

QUESTION:
{query}

ANSWER:"""


def run_rag():
    if not GEMINI_API_KEY:
        print("Error: GEMINI_API_KEY not set.")
        print("Set it with: $env:GEMINI_API_KEY='your-key-here'")
        return

    genai.configure(api_key=GEMINI_API_KEY)
    gemini    = genai.GenerativeModel(GEMINI_MODEL)
    model     = SentenceTransformer(EMBED_MODEL)
    collection = get_collection()

    total = collection.count()
    if total == 0:
        print("Vector DB is empty. Run 'python chunking.py etl' first.")
        return

    print(f"RAG ready — {total} chunks indexed.")
    print("Filters are optional. Press Enter to skip.")
    print("Type 'exit' to quit.\n")

    while True:
        query = input("You: ").strip()
        if query.lower() == "exit":
            break
        if not query:
            continue

        query_embedding = model.encode([query]).tolist()

        results = collection.query(
            query_embeddings = query_embedding,
            n_results        = TOP_K,
            include          = ["documents", "metadatas", "distances"]
        )

        chunks = [
            {"text": doc, "meta": meta}
            for doc, meta in zip(results["documents"][0], results["metadatas"][0])
        ]

        if not chunks:
            print("\nNo relevant chunks found.\n")
            continue

        prompt   = build_prompt(query, chunks)
        response = gemini.generate_content(prompt)

        print(f"\nGemini: {response.text}\n")
        print("-" * 60 + "\n")


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "etl":
        reindex = "--reindex" in args
        run_etl(reindex=reindex)

    elif args[0] == "query":
        run_query()

    elif args[0] == "rag":
        run_rag()

    else:
        print("Usage:")
        print("  python chunking.py etl             # process new PDFs")
        print("  python chunking.py etl --reindex   # reprocess all PDFs")
        print("  python chunking.py query           # raw vector search")
        print("  python chunking.py rag             # RAG chat with Gemini")
