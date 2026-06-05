# smartfile_ui.py

import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from openai import OpenAI

# =========================================
# PAGE CONFIG
# =========================================
st.set_page_config(
    page_title="SmartFile AI",
    page_icon="🤖",
    layout="wide"
)

# =========================================
# HEADER
# =========================================
st.title("🤖 SmartFile AI")
st.markdown("Ask questions about your audit log in plain English — AI converts it to SQL and summarizes the results.")
st.divider()

# =========================================
# SIDEBAR — SETTINGS
# =========================================
with st.sidebar:
    st.header("⚙️ Settings")

    api_key = st.text_input(
        "OpenRouter API Key",
        type="password",
        placeholder="sk-or-v1-xxxxxxxx",
        help="Get your key from https://openrouter.ai/keys"
    )

    st.divider()
    st.subheader("🗄️ Database")

    server_name = st.text_input("Server Name", value=r"AIPLLTH291\SQLEXPRESS_2022")
    database_name = st.text_input("Database Name", value="TESTDB")

    st.divider()
    st.subheader("🤖 Model")
    model_choice = st.selectbox(
        "AI Model",
        ["openai/gpt-3.5-turbo", "openai/gpt-4o", "openai/gpt-4-turbo"],
        index=0
    )

    st.divider()
    st.markdown("📌 [Get OpenRouter API Key](https://openrouter.ai/keys)")

# =========================================
# CONNECTION SETUP
# =========================================
@st.cache_resource
def get_engine(server, database):
    connection_string = (
        f"mssql+pyodbc://@{server}/{database}"
        "?driver=ODBC+Driver+17+for+SQL+Server"
        "&trusted_connection=yes"
    )
    return create_engine(connection_string)


def get_client(key):
    return OpenAI(
        api_key=key,
        base_url="https://openrouter.ai/api/v1"
    )

# =========================================
# PROMPT BUILDER
# =========================================
def build_prompt(question):
    return f"""
You are an SQL expert.
Convert the user's question into SQL.
Table Name:
audits_log
Columns:
AuditID         (int)
FileName        (varchar)
TargetTable     (varchar)
RecordsLoaded   (int)
Status          (varchar, values: 'Success', 'Failed')
ErrorMessage    (varchar)
LoadDate        (datetime)
Rules:
1. Return ONLY raw SQL — no markdown, no code fences, no backticks
2. Do not explain anything
3. SQL Server syntax only
4. For date filtering use: CAST(LoadDate AS DATE) = CAST(GETDATE() AS DATE)
User Question:
{question}
"""

# =========================================
# MAIN UI
# =========================================

# Query History in session
if "history" not in st.session_state:
    st.session_state.history = []

# Input
question = st.text_input(
    "💬 Ask a question about your audit log:",
    placeholder="e.g. Which files failed today? How many records were loaded this week?"
)

col1, col2 = st.columns([1, 5])
with col1:
    run_button = st.button("🚀 Run Query", use_container_width=True)
with col2:
    if st.button("🗑️ Clear History", use_container_width=False):
        st.session_state.history = []
        st.rerun()

# =========================================
# RUN QUERY
# =========================================
if run_button:
    if not api_key:
        st.error("❌ Please enter your OpenRouter API Key in the sidebar.")
    elif not question:
        st.warning("⚠️ Please enter a question.")
    else:
        try:
            client = get_client(api_key)
            engine = get_engine(server_name, database_name)

            # Step 1 — Generate SQL
            with st.spinner("🤖 Generating SQL..."):
                response = client.chat.completions.create(
                    model=model_choice,
                    messages=[{"role": "user", "content": build_prompt(question)}]
                )
                generated_sql = response.choices[0].message.content.strip()

                # Clean markdown fences
                if "```" in generated_sql:
                    generated_sql = generated_sql.split("```")[1]
                    if generated_sql.lower().startswith("sql"):
                        generated_sql = generated_sql[3:]
                    generated_sql = generated_sql.strip()

            st.subheader("📝 Generated SQL")
            st.code(generated_sql, language="sql")

            # Step 2 — Execute SQL
            with st.spinner("⚙️ Running query..."):
                with engine.connect() as conn:
                    result_df = pd.read_sql(text(generated_sql), conn)

            st.subheader("📊 Query Results")
            if result_df.empty:
                st.info("ℹ️ No data returned for this query.")
            else:
                st.dataframe(result_df, use_container_width=True)

                # Download button
                csv = result_df.to_csv(index=False).encode("utf-8")
                st.download_button(
                    label="⬇️ Download Results as CSV",
                    data=csv,
                    file_name="query_result.csv",
                    mime="text/csv"
                )

                # Step 3 — AI Summary
                with st.spinner("✍️ Generating business summary..."):
                    summary_prompt = f"""
                    Summarize this audit result clearly for business users in plain English.
                    Data:
                    {result_df.head(50).to_string(index=False)}
                    """
                    summary_response = client.chat.completions.create(
                        model=model_choice,
                        messages=[{"role": "user", "content": summary_prompt}]
                    )
                    summary = summary_response.choices[0].message.content.strip()

                st.subheader("🧠 AI Business Summary")
                st.success(summary)

            # Save to history
            st.session_state.history.append({
                "question": question,
                "sql": generated_sql,
                "rows": len(result_df)
            })

        except Exception as e:
            st.error(f"❌ Error: {e}")

# =========================================
# QUERY HISTORY
# =========================================
if st.session_state.history:
    st.divider()
    st.subheader("🕓 Query History")
    for i, item in enumerate(reversed(st.session_state.history), 1):
        with st.expander(f"Q{i}: {item['question']} — {item['rows']} row(s)"):
            st.code(item["sql"], language="sql")