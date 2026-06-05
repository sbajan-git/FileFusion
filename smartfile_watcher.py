# smartfile_watcher.py

import time
import os
import json
import pandas as pd
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from sqlalchemy import create_engine, text, inspect
from openai import OpenAI

# =========================================
# CONFIGURATION
# =========================================
CONFIG = {
    "watch_folder": r"C:\Users\sbajan\Documents\SmartFileAI\uploads\incoming",
    "processed_folder": r"C:\Users\sbajan\Documents\SmartFileAI\uploads\processed",
    "failed_folder": r"C:\Users\sbajan\Documents\SmartFileAI\uploads\failed",
    "server_name": r"AIPLLTH291\SQLEXPRESS_2022",
    "database_name": "TESTDB",
    "api_key": "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",  # ← your real key
    "supported_extensions": [".csv", ".xlsx", ".json", ".txt"],
    "process_delay": 3
}

# =========================================
# DB ENGINE
# =========================================
def get_engine():
    conn_str = (
        f"mssql+pyodbc://@{CONFIG['server_name']}/{CONFIG['database_name']}"
        "?driver=ODBC+Driver+17+for+SQL+Server"
        "&trusted_connection=yes"
    )
    return create_engine(conn_str)

# =========================================
# OPENAI CLIENT
# =========================================
def get_client():
    return OpenAI(
        api_key=CONFIG["api_key"],
        base_url="https://openrouter.ai/api/v1"
    )

# =========================================
# READ FILE
# =========================================
def read_file(file_path):
    name = file_path.lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(file_path)
        elif name.endswith(".xlsx"):
            return pd.read_excel(file_path)
        elif name.endswith(".json"):
            return pd.read_json(file_path)
        elif name.endswith(".txt"):
            with open(file_path, "r") as f:
                first_line = f.readline()
            if "\t" in first_line:
                delim = "\t"
            elif "|" in first_line:
                delim = "|"
            else:
                delim = ","
            return pd.read_csv(file_path, delimiter=delim)
    except Exception as e:
        raise Exception(f"File read error: {e}")

# =========================================
# AI METADATA ANALYSIS
# =========================================
def analyze_metadata(df, filename, client):
    sample = df.head(5).to_string(index=False)
    columns = df.dtypes.reset_index()
    columns.columns = ["Column", "Dtype"]
    col_info = columns.to_string(index=False)

    prompt = f"""
You are a data engineering expert.
Analyze this file metadata and return ONLY a JSON object.

File Name: {filename}
Columns and Data Types:
{col_info}

Sample Data:
{sample}

Return ONLY this JSON structure, no explanation, no markdown:
{{
  "suggested_table_name": "table_name_here",
  "columns": [
    {{
      "name": "column_name",
      "sql_type": "VARCHAR(255)",
      "nullable": true,
      "description": "what this column means"
    }}
  ],
  "primary_key": "column_name_or_null",
  "file_summary": "one sentence about what this file contains"
}}
"""
    response = client.chat.completions.create(
        model="openai/gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}]
    )
    raw = response.choices[0].message.content.strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)

# =========================================
# CREATE TABLE SQL
# =========================================
def create_table_sql(metadata):
    table = metadata["suggested_table_name"]
    cols = []
    for col in metadata["columns"]:
        null = "NULL" if col["nullable"] else "NOT NULL"
        cols.append(f"    [{col['name']}] {col['sql_type']} {null}")
    if metadata.get("primary_key"):
        pk = metadata["primary_key"]
        cols.append(f"    CONSTRAINT PK_{table} PRIMARY KEY ([{pk}])")
    col_sql = ",\n".join(cols)
    return f"CREATE TABLE [{table}] (\n{col_sql}\n);"

# =========================================
# LOG TO AUDIT TABLE
# =========================================
def log_audit(engine, filename, table_name, records, status, error=None):
    try:
        with engine.connect() as conn:
            conn.execute(text("""
                IF NOT EXISTS (
                    SELECT * FROM sysobjects WHERE name='audit_log' AND xtype='U'
                )
                CREATE TABLE audit_log (
                    AuditID INT IDENTITY(1,1) PRIMARY KEY,
                    FileName VARCHAR(255),
                    TargetTable VARCHAR(255),
                    RecordsLoaded INT,
                    Status VARCHAR(50),
                    ErrorMessage VARCHAR(MAX),
                    LoadDate DATETIME DEFAULT GETDATE()
                )
            """))
            conn.execute(text("""
                INSERT INTO audit_log
                    (FileName, TargetTable, RecordsLoaded, Status, ErrorMessage, LoadDate)
                VALUES (:fn, :tt, :rl, :st, :em, GETDATE())
            """), {
                "fn": filename,
                "tt": table_name,
                "rl": records,
                "st": status,
                "em": str(error)[:2000] if error else None
            })
            conn.commit()
    except Exception as e:
        print(f"⚠️ Audit log error: {e}")

# =========================================
# PROCESS FILE
# =========================================
def process_file(file_path):
    filename = os.path.basename(file_path)
    print(f"\n{'='*50}")
    print(f"📂 New file detected: {filename}")
    print(f"{'='*50}")

    engine = get_engine()
    client = get_client()
    table_name = "unknown"

    try:
        # Step 1 — Read file
        print("📖 Reading file...")
        df = read_file(file_path)
        print(f"✅ {len(df)} rows, {len(df.columns)} columns")

        # Step 2 — AI Metadata Analysis
        print("🤖 AI analyzing metadata...")
        metadata = analyze_metadata(df, filename, client)
        table_name = metadata["suggested_table_name"]
        print(f"✅ Suggested table: {table_name}")
        print(f"📋 Summary: {metadata['file_summary']}")

        # Step 3 — Rename df columns to AI suggested names
        ai_cols = [c["name"] for c in metadata["columns"]]
        if len(ai_cols) == len(df.columns):
            df.columns = ai_cols

        # Step 4 — Create or recreate table if columns mismatch
        create_sql = create_table_sql(metadata)
        inspector = inspect(engine)
        table_exists = table_name in inspector.get_table_names()

        with engine.connect() as conn:
            if not table_exists:
                print(f"🗄️ Creating table `{table_name}`...")
                conn.execute(text(create_sql))
                conn.commit()
                print(f"✅ Table created.")
            else:
                existing_cols = [c["name"].lower() for c in inspector.get_columns(table_name)]
                incoming_cols = [c.lower() for c in df.columns]
                missing = [c for c in incoming_cols if c not in existing_cols]

                if missing:
                    print(f"⚠️ Column mismatch: {missing}. Recreating table...")
                    conn.execute(text(f"DROP TABLE [{table_name}]"))
                    conn.execute(text(create_sql))
                    conn.commit()
                    print(f"✅ Table recreated with correct columns.")
                else:
                    print(f"ℹ️ Table `{table_name}` exists. Appending.")

        # Step 5 — Load data
        print(f"📤 Loading {len(df)} records into `{table_name}`...")
        df.to_sql(table_name, engine, if_exists="append", index=False, chunksize=500)
        print(f"✅ Data loaded successfully!")

        # Step 6 — AI Summary
        print("🧠 Generating AI summary...")
        summary_prompt = f"""
        Summarize this data load result for business users in 2-3 sentences.
        File: {filename}
        Table: {table_name}
        Records: {len(df)}
        Columns: {', '.join(df.columns.tolist())}
        Sample: {df.head(3).to_string(index=False)}
        """
        summary_response = client.chat.completions.create(
            model="openai/gpt-3.5-turbo",
            messages=[{"role": "user", "content": summary_prompt}]
        )
        summary = summary_response.choices[0].message.content.strip()
        print(f"🧠 Summary: {summary}")

        # Step 7 — Audit log
        log_audit(engine, filename, table_name, len(df), "Success")
        print(f"📋 Audit log updated.")

        # Step 8 — Move to processed folder
        os.makedirs(CONFIG["processed_folder"], exist_ok=True)
        dest = os.path.join(CONFIG["processed_folder"], filename)
        os.rename(file_path, dest)
        print(f"📁 File moved to processed folder.")
        print(f"\n🎉 {filename} processed successfully!")

    except Exception as e:
        print(f"❌ Error processing {filename}: {e}")
        log_audit(engine, filename, table_name, 0, "Failed", e)

        # Move to failed folder
        os.makedirs(CONFIG["failed_folder"], exist_ok=True)
        dest = os.path.join(CONFIG["failed_folder"], filename)
        try:
            os.rename(file_path, dest)
            print(f"📁 File moved to failed folder.")
        except:
            pass

# =========================================
# FILE WATCHER
# =========================================
class FileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        ext = os.path.splitext(event.src_path)[1].lower()
        if ext in CONFIG["supported_extensions"]:
            print(f"\n⏳ Waiting {CONFIG['process_delay']}s for file to finish copying...")
            time.sleep(CONFIG["process_delay"])
            process_file(event.src_path)

# =========================================
# MAIN
# =========================================
if __name__ == "__main__":
    os.makedirs(CONFIG["watch_folder"], exist_ok=True)
    os.makedirs(CONFIG["processed_folder"], exist_ok=True)
    os.makedirs(CONFIG["failed_folder"], exist_ok=True)

    print("=" * 50)
    print("🚀 SmartFile AI Watcher Started")
    print(f"👀 Watching: {CONFIG['watch_folder']}")
    print(f"🗄️  Database: {CONFIG['database_name']} on {CONFIG['server_name']}")
    print(f"📧 Email: Disabled")
    print("=" * 50)
    print("Drop any CSV, Excel, JSON or TXT file into the watch folder...")
    print("Press Ctrl+C to stop.\n")

    observer = Observer()
    observer.schedule(FileHandler(), CONFIG["watch_folder"], recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\n🛑 Watcher stopped.")
    observer.join()