import os
import sys


import re
import json
import sqlite3
from datetime import datetime
import pandas as pd
from dotenv import load_dotenv

# Import Google GenAI dependencies
from google import genai
from google.genai import types

load_dotenv()

# Make emoji-rich output safe on Windows consoles (cp1252 can't encode them).
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# -------------------------------------------------------------
# STEP 0: Load environment & credentials
# -------------------------------------------------------------
# Resolve .env relative to the project so it loads regardless of CWD.
_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
_BASE_DIR = os.path.dirname(_SRC_DIR)
load_dotenv(os.path.join(_BASE_DIR, ".env"))

gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key or gemini_api_key.strip() in ("", "your_key_here"):
    raise ValueError(
        "❌ CRITICAL: GEMINI_API_KEY is missing or still set to the placeholder. "
        "Add a real key from https://aistudio.google.com/apikey to PipelineIQ/.env"
    )
if not gemini_api_key.startswith("AQ."):
    print("⚠️  GEMINI_API_KEY does not look like a valid key (expected 'AQ....'). "
          "If you pasted an OAuth token, the API will reject it with a 400 'API key not valid' error.")

os.environ["GEMINI_API_KEY"] = gemini_api_key

# 🔒 BYPASS CORPORATE FIREWALL: Disable strict global SSL verification
os.environ["PYTHONHTTPSVERIFY"] = "0"
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

# -------------------------------------------------------------
# Resolve paths relative to the project, not the current directory
# -------------------------------------------------------------
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SRC_DIR)
DB_PATH = os.path.join(BASE_DIR, "data", "source_warehouse.db")
OUT_PATH = os.path.join(BASE_DIR, "data", "pipeline_output.csv")
# Folder scanned for external source files (CSV/JSON/Excel). Overridable via .env.
SOURCES_DIR = os.path.join(BASE_DIR, os.getenv("SOURCES_DIR", os.path.join("data", "sources")))
# Folder where built pipelines are persisted so they can be listed / fixed / changed later.
PIPELINES_DIR = os.path.join(BASE_DIR, "data", "pipelines")
# How many times the self-healing loop re-asks the model after a SQL error.
MAX_REPAIR_ATTEMPTS = int(os.getenv("MAX_REPAIR_ATTEMPTS", "3"))

# Model fallback order — first one that returns text wins. Shared by every LLM call.
_MODELS = ('gemini-2.5-flash', 'models/gemini-2.5-flash',
           'models/gemini-2.5-pro', 'models/gemini-2.5-flash-lite')


def _generate(system_instruction: str, user_content: str, schema: dict,
              temperature: float = 0.1) -> str | None:
    """Call Gemini with the shared model-fallback loop; return raw response text or None.

    Centralizes the try-each-model pattern used by planning, routing, analysis and repair.
    """
    client = genai.Client()
    last_error = None
    for model in _MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=user_content,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
            if response.text:
                return response.text
        except Exception as e:
            last_error = e
            print(f"   ↳ {model} failed: {e}")
    if last_error:
        print(f"   ↳ all models failed. Last error: {last_error}")
    return None


# -------------------------------------------------------------
# STEP 1: Inspect the source database schema
# -------------------------------------------------------------
def fetch_schema(db_path: str = DB_PATH) -> str:
    """Connect to the SQLite database and return the schema layout of all tables."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table';")
        tables = cursor.fetchall()
        conn.close()


        schema_info = "Database Schema Layout:\n"
        for table_name, sql in tables:
            schema_info += f"\nTable: {table_name}\nDDL:\n{sql}\n"
        return schema_info
    except Exception as e:
        return f"Error fetching schema: {str(e)}"


# -------------------------------------------------------------
# STEP 1b: Discover external source files (CSV / JSON / Excel)
# -------------------------------------------------------------
def _read_source_file(path: str) -> pd.DataFrame:
    """Read a source file into a DataFrame based on its extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return pd.read_csv(path)
    if ext in (".json",):
        return pd.read_json(path)
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path)
    raise ValueError(f"Unsupported source file type: {ext}")


def discover_sources(sources_dir: str = SOURCES_DIR) -> str:
    """Scan the sources folder and describe each file (name + columns + sample rows).

    Returns a compact, model-friendly description the planner can reason over.
    """
    if not os.path.isdir(sources_dir):
        return "Available source files: (none — no sources folder found)\n"

    supported = (".csv", ".json", ".xlsx", ".xls")
    files = sorted(f for f in os.listdir(sources_dir)
                   if os.path.splitext(f)[1].lower() in supported)
    if not files:
        return "Available source files: (none)\n"

    info = "Available source files (in the sources folder):\n"
    for fname in files:
        path = os.path.join(sources_dir, fname)
        try:
            df = _read_source_file(path)
            cols = ", ".join(str(c) for c in df.columns)
            sample = df.head(2).to_dict(orient="records")
            info += f"\nFile: {fname}\nColumns: {cols}\nSample rows: {sample}\n"
        except Exception as e:
            info += f"\nFile: {fname}\n(could not read preview: {e})\n"
    return info


# -------------------------------------------------------------
# STEP 1c: Pipeline registry — persist built pipelines so they can be
#          listed, re-run, fixed, and changed later.
# -------------------------------------------------------------
def _pipeline_path(name: str) -> str:
    """Resolve the JSON file backing a pipeline, sanitizing the name for the filesystem."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip("_") or "pipeline"
    return os.path.join(PIPELINES_DIR, f"{safe}.json")


def save_pipeline(name: str, request: str, plan: dict, status: str,
                  error: str | None = None) -> None:
    """Write or update a pipeline record. Keyed by the plan's output_table name."""
    os.makedirs(PIPELINES_DIR, exist_ok=True)
    path = _pipeline_path(name)
    now = datetime.now().isoformat(timespec="seconds")
    created_at = now
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                created_at = json.load(f).get("created_at", now)
        except Exception:
            pass
    record = {
        "name": name,
        "request": request,
        "plan": plan,
        "created_at": created_at,
        "updated_at": now,
        "last_status": status,
        "last_error": error,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)


def load_pipeline(name: str) -> dict | None:
    """Load a single pipeline record by name, or None if it doesn't exist."""
    path = _pipeline_path(name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_pipelines() -> list:
    """Return all saved pipeline records, most recently updated first."""
    if not os.path.isdir(PIPELINES_DIR):
        return []
    records = []
    for fname in os.listdir(PIPELINES_DIR):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(PIPELINES_DIR, fname), "r", encoding="utf-8") as f:
                records.append(json.load(f))
        except Exception:
            continue
    records.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
    return records


def resolve_pipeline_name(user_text: str) -> str | None:
    """Best-effort match of free text to a saved pipeline name.

    Tries an exact name match first, then a substring match against the request
    text, so 'fix the customer summary' can find the 'customer_orders' pipeline.
    Returns the single best match, or None when nothing matches confidently.
    """
    records = list_pipelines()
    if not records:
        return None
    text = (user_text or "").lower()
    # 1) Exact name token appears in the text.
    for r in records:
        if r["name"].lower() in text:
            return r["name"]
    # 2) Substring overlap with the original request (loose match).
    for r in records:
        if r.get("request") and r["request"].lower() in text:
            return r["name"]
    # 3) Only one pipeline exists — assume they mean that one.
    if len(records) == 1:
        return records[0]["name"]
    return None


# -------------------------------------------------------------
# STEP 2: Generate a structured ETL plan from the user's request
# -------------------------------------------------------------
# JSON schema describing the plan we ask Gemini to return.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "ingestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "table": {"type": "string"},
                },
                "required": ["file", "table"],
            },
        },
        "sql_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "sql": {"type": "string"},
                },
                "required": ["description", "sql"],
            },
        },
        "output_table": {"type": "string"},
    },
    "required": ["summary", "ingestions", "sql_steps", "output_table"],
}

_PLAN_RULES = (
    "Rules:\n"
    "- 'ingestions': only list files that appear in the available source files. Each file "
    "is loaded into a new SQLite table you name (use a clear *_raw name). Leave empty if no "
    "files are needed.\n"
    "- After ingestion, those tables exist and can be referenced in 'sql_steps'.\n"
    "- 'sql_steps': an ordered list of valid SQLite statements. They run sequentially in one "
    "transaction. To merge multiple tables into a single table, emit "
    "'CREATE TABLE <name> AS SELECT ...' (use explicit joins and correct column names), or a "
    "'CREATE TABLE' followed by 'INSERT INTO <name> SELECT ...'. Use 'DROP TABLE IF EXISTS' "
    "before re-creating a table so reruns are idempotent.\n"
    "- 'output_table': the final single table to deliver. It MUST be created by one of the "
    "sql_steps.\n"
    "- Use only column/table names that exist in the schema or in your ingestion/sql steps.\n"
    "Return ONLY the JSON object matching the requested schema — no prose, no markdown fences."
)


def generate_plan(user_prompt: str, schema: str, sources_info: str) -> dict:
    """Ask Gemini to turn the user's request into a structured, executable ETL plan."""
    system_instruction = (
        "You are an expert SQLite data engineer that designs executable ETL plans. "
        "Given the existing database schema, a list of available external source files, "
        "and a natural-language request, produce a JSON plan that, when executed in order, "
        "satisfies the request.\n" + _PLAN_RULES
    )
    user_content = (
        f"{schema}\n\n"
        f"{sources_info}\n\n"
        f"User request: {user_prompt}\n\n"
        "Produce the JSON ETL plan:"
    )

    print("🤖 Designing pipeline plan...")
    raw = _generate(system_instruction, user_content, PLAN_SCHEMA, temperature=0.1)
    if not raw:
        raise RuntimeError("❌ All Gemini models failed to generate a plan.")
    return parse_plan(raw)


def parse_plan(raw: str) -> dict:
    """Parse the model's JSON plan, tolerating stray markdown fences."""
    text = raw.strip()
    if "```" in text:
        # Pull out the fenced block that looks like JSON.
        for part in text.split("```"):
            part = part.strip()
            if part.lower().startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    return json.loads(text)


# -------------------------------------------------------------
# STEP 2b: Route the request so each ask reaches the right capability.
# -------------------------------------------------------------
# Enum-constrained schema so the model picks exactly one capability and,
# for fix/change, names which saved pipeline it means.
ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["build", "analyze", "fix", "change", "list"],
        },
        "target_pipeline": {"type": "string"},
    },
    "required": ["intent"],
}


def route_request(user_prompt: str, schema: str, pipeline_names: list) -> dict:
    """Classify the request into a capability and (optionally) a target pipeline.

    Intents:
      - build:   create a brand-new analysis-ready table / ETL pipeline.
      - analyze: answer a read-only question about existing data.
      - fix:     repair an existing saved pipeline that is failing.
      - change:  modify an existing saved pipeline's logic.
      - list:    show saved pipelines and the current schema.

    Defaults to 'build' on any failure so the gate never makes the app less functional.
    """
    names = ", ".join(pipeline_names) if pipeline_names else "(none yet)"
    system_instruction = (
        "You route a user's data request to exactly one capability.\n"
        "- 'build': create, transform, merge, clean, or deliver a NEW analysis-ready table / ETL pipeline.\n"
        "- 'analyze': answer a read-only question about existing data (totals, top-N, counts, "
        "lookups) — they want an ANSWER, not a new stored table.\n"
        "- 'fix': repair an EXISTING saved pipeline that is broken or failing.\n"
        "- 'change': modify the logic of an EXISTING saved pipeline (add a column, change a filter).\n"
        "- 'list': show what pipelines/tables already exist.\n"
        "For 'fix' and 'change', set 'target_pipeline' to the saved pipeline name they mean "
        "(choose from the known pipelines), or leave it empty if unclear."
    )
    user_content = (
        f"{schema}\n\n"
        f"Known saved pipelines: {names}\n\n"
        f"User request: {user_prompt}\n\n"
        "Route the request:"
    )

    raw = _generate(system_instruction, user_content, ROUTE_SCHEMA, temperature=0)
    if raw:
        try:
            data = json.loads(raw)
            if data.get("intent") in ("build", "analyze", "fix", "change", "list"):
                return {"intent": data["intent"],
                        "target_pipeline": (data.get("target_pipeline") or "").strip()}
        except Exception:
            pass
    # Fail open: preserve the existing build behavior rather than blocking.
    return {"intent": "build", "target_pipeline": ""}


# -------------------------------------------------------------
# STEP 2c: Analysis — answer read-only questions with real results.
# -------------------------------------------------------------
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["sql", "explanation"],
}

# Statements that would mutate the warehouse — never allowed on the analysis path.
_WRITE_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|REPLACE|ATTACH|PRAGMA)\b",
                       re.IGNORECASE)


def _gen_analysis_sql(question: str, schema: str, prior_error: str = "") -> dict | None:
    """Ask Gemini for a single read-only SELECT (plus an explanation) for a question."""
    system_instruction = (
        "You are a SQLite analyst. Given the database schema and a question, return a SINGLE "
        "read-only SELECT statement that answers it, plus a short plain-language explanation of "
        "what it computes.\n"
        "Rules:\n"
        "- Exactly one statement, and it MUST be a SELECT (optionally a WITH ... SELECT). "
        "Never INSERT/UPDATE/DELETE/CREATE/DROP/ALTER.\n"
        "- Use only tables and columns that exist in the schema.\n"
        "- Prefer clear column aliases and sensible ordering/limits."
    )
    user_content = f"{schema}\n\nQuestion: {question}\n"
    if prior_error:
        user_content += f"\nThe previous query failed with: {prior_error}\nFix it.\n"
    user_content += "\nReturn the SELECT and explanation:"

    raw = _generate(system_instruction, user_content, ANALYSIS_SCHEMA, temperature=0)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def answer_analysis(question: str, schema: str, db_path: str = DB_PATH) -> None:
    """Generate a guarded read-only query, run it, and print the answer.

    On a SQL error, re-asks the model once with the error before giving up.
    Never mutates the warehouse.
    """
    print("\n🔍 Answering your question...")
    prior_error = ""
    for attempt in range(2):
        result = _gen_analysis_sql(question, schema, prior_error)
        if not result:
            print("❌ Could not generate a query for that question.")
            return

        sql = (result.get("sql") or "").strip().rstrip(";")
        explanation = result.get("explanation", "")

        if not sql.lower().lstrip().startswith(("select", "with")) or _WRITE_RE.search(sql):
            print("🛑 Refused: analysis only runs read-only SELECT queries.\n"
                  f"   (model proposed: {sql[:120]}...)")
            return

        print(f"\n🧠 {explanation}")
        print(f"📜 SQL: {sql}")
        try:
            conn = sqlite3.connect(db_path)
            df = pd.read_sql_query(sql, conn)
            conn.close()
            print("\n📊 Result:")
            print(df.to_markdown(index=False) if not df.empty else "   (no rows)")
            return
        except Exception as e:
            prior_error = str(e)
            print(f"\n⚠️  Query failed ({prior_error}).")
            if attempt == 0:
                print("   ↻ Re-asking the model with the error...")
    print("❌ Could not answer that question after a retry.")


# -------------------------------------------------------------
# STEP 3: Show the plan and ask for confirmation
# -------------------------------------------------------------
def display_plan(plan: dict) -> None:
    """Print the planned summary, ingestions, and every SQL step for review."""
    print("\n" + "=" * 50)
    print("🛠️  PROPOSED PIPELINE PLAN:")
    print("=" * 50)
    print(f"\n📋 Summary: {plan.get('summary', '(no summary)')}")

    ingestions = plan.get("ingestions") or []
    if ingestions:
        print("\n📥 Source files to ingest:")
        for ing in ingestions:
            print(f"   • {ing['file']}  →  table '{ing['table']}'")
    else:
        print("\n📥 Source files to ingest: (none — using existing tables only)")

    print("\n🧱 SQL steps (run in order):")
    for i, step in enumerate(plan.get("sql_steps") or [], start=1):
        print(f"\n   Step {i}: {step.get('description', '')}")
        print("   " + step["sql"].replace("\n", "\n   "))

    print(f"\n🎯 Final output table: {plan.get('output_table', '(unspecified)')}")
    print("=" * 50)


def confirm() -> bool:
    """Ask the user to approve the full plan once before any execution."""
    answer = input("\n❓ Proceed with this plan? (y/n): ").strip().lower()
    return answer in ("y", "yes")


# -------------------------------------------------------------
# STEP 4: Execute the approved plan (with self-healing repair)
# -------------------------------------------------------------
def ingest_file(path: str, table: str, conn: sqlite3.Connection) -> int:
    """Load a source file into SQLite as `table`, replacing any existing one."""
    df = _read_source_file(path)
    df.to_sql(table, conn, if_exists="replace", index=False)
    return len(df)


def execute_plan(plan: dict, db_path: str = DB_PATH,
                 sources_dir: str = SOURCES_DIR) -> pd.DataFrame:
    """Run ingestions + ordered SQL steps and return the final output DataFrame.

    Raises on any failure (after rolling back) so the self-healing loop can catch it.
    """
    conn = sqlite3.connect(db_path)
    try:
        # 1) Ingest external source files into raw tables.
        for ing in plan.get("ingestions") or []:
            src_path = os.path.join(sources_dir, ing["file"])
            rows = ingest_file(src_path, ing["table"], conn)
            print(f"📥 Ingested {rows} rows from '{ing['file']}' into table '{ing['table']}'")

        # 2) Run the ordered DDL/DML steps in a single transaction.
        for i, step in enumerate(plan.get("sql_steps") or [], start=1):
            print(f"🧱 Running step {i}: {step.get('description', '')}")
            conn.execute(step["sql"])
        conn.commit()

        # 3) Read back the final single table.
        output_table = plan["output_table"]
        df = pd.read_sql_query(f"SELECT * FROM {output_table}", conn)
        return df
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def deliver(df: pd.DataFrame, output_table: str) -> None:
    """Print the final table and save it to the delivery CSV."""
    print("\n🏆 TARGET ANALYSIS-READY OUTPUT DATA:")
    print(df.to_markdown(index=False) if not df.empty else "   (no rows)")
    df.to_csv(OUT_PATH, index=False)
    print(f"\n💾 Final table '{output_table}' saved to the database and delivered to '{OUT_PATH}'")


def repair_plan(plan: dict, error: str, schema: str, sources_info: str,
                request: str) -> dict | None:
    """Ask Gemini to fix a failing ETL plan given the exact SQL error. Returns a new plan."""
    system_instruction = (
        "You are an expert SQLite data engineer FIXING a failing ETL plan. "
        "You are given the original request, the current JSON plan, the database schema, the "
        "available source files, and the exact error raised when the plan ran. Diagnose the "
        "cause and return a corrected JSON plan that runs cleanly.\n" + _PLAN_RULES
    )
    user_content = (
        f"{schema}\n\n"
        f"{sources_info}\n\n"
        f"Original request: {request}\n\n"
        f"Current plan (JSON):\n{json.dumps(plan, indent=2)}\n\n"
        f"It failed with this error:\n{error}\n\n"
        "Return the corrected JSON ETL plan:"
    )
    raw = _generate(system_instruction, user_content, PLAN_SCHEMA, temperature=0.1)
    if not raw:
        return None
    try:
        return parse_plan(raw)
    except Exception:
        return None


def run_with_repair(plan: dict, request: str, schema: str, sources_info: str,
                    max_attempts: int = MAX_REPAIR_ATTEMPTS,
                    seed_error: str = "") -> tuple:
    """Execute a plan, auto-repairing via the model on failure.

    Returns (final_plan, df_or_None, status, error). On the first attempt it can be
    seeded with a known prior error (used by 'fix') so the model starts from the cause.
    """
    error = seed_error
    for attempt in range(1, max_attempts + 1):
        if error:
            print(f"\n🩹 Repair attempt {attempt}/{max_attempts} (cause: {error[:120]})")
            repaired = repair_plan(plan, error, schema, sources_info, request)
            if not repaired:
                print("   ↳ could not produce a repaired plan.")
                return plan, None, "failed", error
            plan = repaired
            display_plan(plan)
        try:
            df = execute_plan(plan)
            deliver(df, plan["output_table"])
            return plan, df, "success", None
        except Exception as e:
            error = str(e)
            print(f"\n❌ Execution failed: {error}")
    print(f"\n🛑 Gave up after {max_attempts} attempt(s).")
    return plan, None, "failed", error


# -------------------------------------------------------------
# STEP 5: Change & fix existing pipelines
# -------------------------------------------------------------
def modify_plan(existing_plan: dict, change_request: str, schema: str,
                sources_info: str) -> dict:
    """Ask Gemini to apply a natural-language change to an existing plan."""
    system_instruction = (
        "You are an expert SQLite data engineer MODIFYING an existing ETL plan. "
        "You are given the current JSON plan, the schema, the available source files, and a "
        "change request. Return the FULL updated JSON plan that incorporates the change while "
        "keeping everything else intact.\n" + _PLAN_RULES
    )
    user_content = (
        f"{schema}\n\n"
        f"{sources_info}\n\n"
        f"Current plan (JSON):\n{json.dumps(existing_plan, indent=2)}\n\n"
        f"Change request: {change_request}\n\n"
        "Return the full updated JSON ETL plan:"
    )
    print("🤖 Applying your change...")
    raw = _generate(system_instruction, user_content, PLAN_SCHEMA, temperature=0.1)
    if not raw:
        raise RuntimeError("❌ All Gemini models failed to modify the plan.")
    return parse_plan(raw)


def change_pipeline(name: str, change_request: str, schema: str, sources_info: str) -> None:
    """Load a saved pipeline, apply a change, confirm, run, and persist the result."""
    record = load_pipeline(name)
    if not record:
        print(f"❌ No saved pipeline named '{name}'. Try 'list' to see what exists.")
        return
    plan = modify_plan(record["plan"], change_request, schema, sources_info)
    display_plan(plan)
    if not confirm():
        print("\n🛑 Change rejected. The saved pipeline is unchanged.")
        return
    new_request = f"{record['request']} | change: {change_request}"
    plan, _df, status, error = run_with_repair(plan, new_request, schema, sources_info)
    save_pipeline(plan.get("output_table", name), new_request, plan, status, error)
    if status == "success":
        print(f"\n✅ Pipeline '{plan.get('output_table', name)}' updated.")


def fix_pipeline(name: str, schema: str, sources_info: str, hint: str = "") -> None:
    """Reload a saved pipeline and run the self-healing loop to repair it."""
    record = load_pipeline(name)
    if not record:
        print(f"❌ No saved pipeline named '{name}'. Try 'list' to see what exists.")
        return
    seed = hint or record.get("last_error") or ""
    if not seed:
        # Nothing known to be wrong — execute once; repair only kicks in if it actually fails.
        print(f"ℹ️  No recorded error for '{name}'. Re-running it to verify...")
    plan, _df, status, error = run_with_repair(
        record["plan"], record["request"], schema, sources_info, seed_error=seed)
    save_pipeline(plan.get("output_table", name), record["request"], plan, status, error)
    if status == "success":
        print(f"\n✅ Pipeline '{plan.get('output_table', name)}' is healthy.")


# -------------------------------------------------------------
# STEP 6: Dispatch a single request to the right capability
# -------------------------------------------------------------
def show_list(schema: str) -> None:
    """Print saved pipelines and the current database schema."""
    records = list_pipelines()
    print("\n" + "=" * 50)
    print("📦 SAVED PIPELINES")
    print("=" * 50)
    if not records:
        print("(none yet — build one to get started)")
    else:
        for r in records:
            flag = "✅" if r.get("last_status") == "success" else "⚠️ "
            print(f" {flag} {r['name']}  —  updated {r.get('updated_at', '?')}")
            print(f"      request: {r.get('request', '')}")
    print("\n" + schema)


def handle_request(user_prompt: str) -> None:
    """Route one natural-language request and run the matching capability."""
    schema = fetch_schema()
    names = [r["name"] for r in list_pipelines()]
    route = route_request(user_prompt, schema, names)
    intent = route["intent"]
    target = route.get("target_pipeline", "")
    print(f"🧭 Intent: {intent}" + (f" → '{target}'" if target else ""))

    if intent == "list":
        show_list(schema)
        return

    if intent == "analyze":
        answer_analysis(user_prompt, schema)
        return

    sources_info = discover_sources()

    if intent == "change":
        name = target or resolve_pipeline_name(user_prompt)
        if not name:
            print("❓ Which pipeline should I change? Run 'list' to see them.")
            return
        change_pipeline(name, user_prompt, schema, sources_info)
        return

    if intent == "fix":
        name = target or resolve_pipeline_name(user_prompt)
        if not name:
            print("❓ Which pipeline should I fix? Run 'list' to see them.")
            return
        fix_pipeline(name, schema, sources_info)
        return

    # Default: build a new pipeline.
    plan = generate_plan(user_prompt, schema, sources_info)
    display_plan(plan)
    if not confirm():
        print("\n🛑 Plan rejected. No changes were made.")
        return
    print("\n💾 Executing approved pipeline...")
    plan, _df, status, error = run_with_repair(plan, user_prompt, schema, sources_info)
    save_pipeline(plan.get("output_table", "pipeline"), user_prompt, plan, status, error)


# -------------------------------------------------------------
# STEP 7: Interactive REPL — talk to PipelineIQ like a data engineer
# -------------------------------------------------------------
HELP_TEXT = """
🛠️  PipelineIQ — your AI data engineer. I can:
  • build    — "merge users and orders with product details into a customer summary"
  • analyze  — "which user has spent the most in total?"
  • change   — "change customer_orders to also include product category"
  • fix      — "fix the customer_orders pipeline"
  • list     — show saved pipelines and the current schema

Commands: help · list · quit / exit
"""


def repl() -> None:
    """Run the interactive prompt loop until the user quits."""
    print("=" * 50)
    print("🤖 PipelineIQ powered by Gemini — type 'help' for what I can do.")
    print("=" * 50)
    print(HELP_TEXT)
    while True:
        try:
            user_prompt = input("\n🛠️  PipelineIQ ▸ ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n👋 Bye!")
            return
        if not user_prompt:
            continue
        low = user_prompt.lower()
        if low in ("quit", "exit", "q"):
            print("👋 Bye!")
            return
        if low in ("help", "?"):
            print(HELP_TEXT)
            continue
        if low == "list":
            show_list(fetch_schema())
            continue
        try:
            handle_request(user_prompt)
        except Exception as e:
            print(f"\n❌ Something went wrong handling that request: {e}")


if __name__ == "__main__":
    # Backward-compat: a request passed as CLI args runs once, then we drop into the REPL.
    if len(sys.argv) > 1:
        initial = " ".join(sys.argv[1:]).strip()
        print(f"\n🚀 Request: '{initial}'\n")
        try:
            handle_request(initial)
        except Exception as e:
            print(f"\n❌ Something went wrong handling that request: {e}")
    repl()
