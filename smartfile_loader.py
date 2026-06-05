# smartfile_loader.py

import streamlit as st
import pandas as pd
import json
import os
from sqlalchemy import create_engine, text, inspect
from openai import OpenAI

# =========================================
# PAGE CONFIG
# =========================================
st.set_page_config(
    page_title="SmartFile AI Loader",
    page_icon="📂",
    layout="wide"
)

st.title("📂 SmartFile AI Loader")
st.markdown("Upload or pick a file — AI reads the metadata and loads it into SQL Server automatically.")
st.divider()

# =========================================
# SIDEBAR — SETTINGS
# =========================================
with st.sidebar:
    st.header("⚙️ Settings")

    api_key = st.text_input(
        "",
        type="password",
        placeholder="sk-or-v1-xxxxxxxx"
    )

    st.divider()
    st.subheader("🗄️ Database")
    server_name = st.text_input("Server Name", value=r"AIPLLTH291\SQLEXPRESS_2022")
    database_name = st.text_input("Database Name", value="TESTDB")

    st.divider()
    st.subheader("📋 Load Mode")
    load_mode = st.radio(
        "If table exists:",
        ["Append", "Replace", "Skip"]
    )

    st.divider()
    st.markdown("📌 [Get OpenRouter API Key](https://openrouter.ai/keys)")

# =========================================
# DB ENGINE
# =========================================
@st.cache_resource
def get_engine(server, database):
    conn_str = (
        f"mssql+pyodbc://@{server}/{database}"
        "?driver=ODBC+Driver+17+for+SQL+Server"
        "&trusted_connection=yes"
    )
    return create_engine(conn_str)

# =========================================
# OPENAI CLIENT
# =========================================
def get_client(key):
    return OpenAI(
        api_key=key,
        base_url="https://openrouter.ai/api/v1"
    )

# =========================================
# READ FILE INTO DATAFRAME
# =========================================
def read_file(file_path=None, uploaded_file=None):
    try:
        if uploaded_file:
            name = uploaded_file.name.lower()
            if name.endswith(".csv"):
                return pd.read_csv(uploaded_file), uploaded_file.name
            elif name.endswith(".xlsx"):
                return pd.read_excel(uploaded_file), uploaded_file.name
            elif name.endswith(".json"):
                return pd.read_json(uploaded_file), uploaded_file.name
            elif name.endswith(".txt"):
                # Auto-detect delimiter
                content = uploaded_file.read().decode("utf-8")
                uploaded_file.seek(0)
                if "\t" in content.split("\n")[0]:
                    delim = "\t"
                elif "|" in content.split("\n")[0]:
                    delim = "|"
                else:
                    delim = ","  # default to comma
                return pd.read_csv(uploaded_file, delimiter=delim), uploaded_file.name

        elif file_path:
            name = file_path.lower()
            if name.endswith(".csv"):
                return pd.read_csv(file_path), os.path.basename(file_path)
            elif name.endswith(".xlsx"):
                return pd.read_excel(file_path), os.path.basename(file_path)
            elif name.endswith(".json"):
                return pd.read_json(file_path), os.path.basename(file_path)
            elif name.endswith(".txt"):
                # Auto-detect delimiter
                with open(file_path, "r") as f:
                    first_line = f.readline()
                if "\t" in first_line:
                    delim = "\t"
                elif "|" in first_line:
                    delim = "|"
                else:
                    delim = ","  # default to comma
                return pd.read_csv(file_path, delimiter=delim), os.path.basename(file_path)

    except Exception as e:
        st.error(f"❌ Error reading file: {e}")
        return None, None

# =========================================
# AI METADATA ANALYSIS
# =========================================
def analyze_metadata(df, filename, client, model="openai/gpt-3.5-turbo"):
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
        model=model,
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
                    ErrorMessage VARCHAR(1000),
                    LoadDate DATETIME DEFAULT GETDATE()
                )
            """))
            conn.execute(text("""
                INSERT INTO audit_log
                    (FileName, TargetTable, RecordsLoaded, Status, ErrorMessage, LoadDate)
                VALUES
                    (:fn, :tt, :rl, :st, :em, GETDATE())
            """), {
                "fn": filename,
                "tt": table_name,
                "rl": records,
                "st": status,
                "em": str(error) if error else None
            })
            conn.commit()
    except Exception as e:
        st.warning(f"⚠️ Audit log error: {e}")

# =========================================
# CORE LOAD FUNCTION  ← defined BEFORE tabs
# =========================================
def _run_load(df, filename, api_key, server, database, load_mode):
    client = get_client(api_key)
    engine = get_engine(server, database)

    # Step 1 — AI Metadata Analysis
    with st.spinner("🤖 AI analyzing file metadata..."):
        try:
            metadata = analyze_metadata(df, filename, client)
        except Exception as e:
            st.error(f"❌ AI metadata error: {e}")
            return

    table_name = metadata["suggested_table_name"]

    st.subheader("🧠 AI Metadata Analysis")
    st.info(f"📋 **File Summary:** {metadata['file_summary']}")
    st.info(f"🗄️ **Suggested Table:** `{table_name}`")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**📊 Detected Columns:**")
        col_df = pd.DataFrame([{
            "Column": c["name"],
            "SQL Type": c["sql_type"],
            "Nullable": c["nullable"],
            "Description": c["description"]
        } for c in metadata["columns"]])
        st.dataframe(col_df, use_container_width=True)

    with col2:
        create_sql = create_table_sql(metadata)
        st.markdown("**📝 Generated CREATE TABLE:**")
        st.code(create_sql, language="sql")

    # Step 2 — Create or Check Table
    with st.spinner("⚙️ Creating/checking table in SQL Server..."):
        try:
            inspector = inspect(engine)
            table_exists = table_name in inspector.get_table_names()

            with engine.connect() as conn:
                if not table_exists:
                    conn.execute(text(create_sql))
                    conn.commit()
                    st.success(f"✅ Table `{table_name}` created successfully.")
                else:
                    if load_mode == "Replace":
                        conn.execute(text(f"DROP TABLE [{table_name}]"))
                        conn.execute(text(create_sql))
                        conn.commit()
                        st.success(f"✅ Table `{table_name}` replaced successfully.")
                    elif load_mode == "Skip":
                        st.warning(f"⚠️ Table `{table_name}` already exists. Skipping.")
                        return
                    else:
                        st.info(f"ℹ️ Table `{table_name}` exists. Appending data.")
        except Exception as e:
            st.error(f"❌ Table creation error: {e}")
            log_audit(engine, filename, table_name, 0, "Failed", e)
            return

    # Step 3 — Load Data
    with st.spinner(f"📤 Loading {len(df)} records into `{table_name}`..."):
        try:
            ai_cols = [c["name"] for c in metadata["columns"]]
            if len(ai_cols) == len(df.columns):
                df.columns = ai_cols

            df.to_sql(
                table_name,
                engine,
                if_exists="append",
                index=False,
                chunksize=500
            )
            st.success(f"✅ {len(df)} records loaded into `{table_name}` successfully!")
            log_audit(engine, filename, table_name, len(df), "Success")

        except Exception as e:
            st.error(f"❌ Data load error: {e}")
            log_audit(engine, filename, table_name, 0, "Failed", e)
            return

    # Step 4 — Preview loaded data
    st.subheader("📊 Loaded Data Preview")
    with engine.connect() as conn:
        preview_df = pd.read_sql(
            text(f"SELECT TOP 10 * FROM [{table_name}]"), conn
        )
    st.dataframe(preview_df, use_container_width=True)
    st.balloons()

# =========================================
# TABS — defined AFTER _run_load
# =========================================
tab1, tab2 = st.tabs(["📤 Upload File", "📁 Load from Folder Path"])

# TAB 1 — UPLOAD FILE
with tab1:
    uploaded_file = st.file_uploader(
        "Drop your file here",
        type=["csv", "xlsx", "json", "txt"]
    )

    if uploaded_file:
        df, filename = read_file(uploaded_file=uploaded_file)
        if df is not None:
            st.success(f"✅ File loaded: **{filename}** — {len(df)} rows, {len(df.columns)} columns")
            st.dataframe(df.head(10), use_container_width=True)

            if st.button("🤖 Analyze & Load to SQL", key="upload_btn"):
                if not api_key:
                    st.error("❌ Enter your OpenRouter API Key in the sidebar.")
                else:
                    _run_load(df, filename, api_key, server_name, database_name, load_mode)

# TAB 2 — FOLDER PATH
with tab2:
    folder_path = st.text_input(
        "Enter folder path:",
        placeholder=r"C:\Users\sbajan\Documents\SmartFileAI\uploads\data"
    )

    if folder_path and os.path.exists(folder_path):
        files = [
            f for f in os.listdir(folder_path)
            if f.endswith((".csv", ".xlsx", ".json", ".txt"))
        ]
        if files:
            selected_file = st.selectbox("Select a file to load:", files)
            full_path = os.path.join(folder_path, selected_file)
            df, filename = read_file(file_path=full_path)

            if df is not None:
                st.success(f"✅ File loaded: **{filename}** — {len(df)} rows, {len(df.columns)} columns")
                st.dataframe(df.head(10), use_container_width=True)

                if st.button("🤖 Analyze & Load to SQL", key="folder_btn"):
                    if not api_key:
                        st.error("❌ Enter your OpenRouter API Key in the sidebar.")
                    else:
                        _run_load(df, filename, api_key, server_name, database_name, load_mode)
        else:
            st.warning("⚠️ No supported files found in that folder.")
    elif folder_path:
        st.error("❌ Folder path does not exist.")