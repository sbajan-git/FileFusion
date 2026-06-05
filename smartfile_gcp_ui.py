# smartfile_gcp_ui.py

import streamlit as st
import pandas as pd
import time
import os
import io
from datetime import datetime
from google.cloud import storage, bigquery

# =========================================
# PAGE CONFIG
# =========================================
st.set_page_config(
    page_title="SmartFile GCP",
    page_icon="☁️",
    layout="wide"
)

# =========================================
# CONFIG
# =========================================
PROJECT_ID  = "gcp-filefusion"
DATASET_ID  = "smartfile_data"
BUCKET_NAME = "smartfile-incoming-gcp-filefusion"

# =========================================
# CLIENTS
# =========================================
@st.cache_resource
def get_gcs_client():
    return storage.Client()

@st.cache_resource
def get_bq_client():
    return bigquery.Client(project=PROJECT_ID)

# =========================================
# HEADER
# =========================================
st.title("☁️ SmartFile GCP Dashboard")
st.markdown("Upload files, monitor GCS, and view BigQuery data — all in one place.")
st.divider()

# =========================================
# SIDEBAR
# =========================================
with st.sidebar:
    st.header("⚙️ Settings")
    st.info(f"**Project:** {PROJECT_ID}")
    st.info(f"**Dataset:** {DATASET_ID}")
    st.info(f"**Bucket:** {BUCKET_NAME}")
    st.divider()
    auto_refresh = st.toggle("🔄 Auto Refresh (10s)", value=False)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.markdown("🔗 [GCS Console](https://console.cloud.google.com/storage/browser/smartfile-incoming-gcp-filefusion?project=gcp-filefusion)")
    st.markdown("🔗 [BigQuery Console](https://console.cloud.google.com/bigquery?project=gcp-filefusion)")
    st.markdown("🔗 [Cloud Functions](https://console.cloud.google.com/functions/details/asia-south1/smartfile-loader?project=gcp-filefusion)")

# =========================================
# HELPER FUNCTIONS
# =========================================
@st.cache_data(ttl=10)
def list_gcs_files(prefix):
    try:
        gcs = get_gcs_client()
        bucket = gcs.bucket(BUCKET_NAME)
        blobs = list(bucket.list_blobs(prefix=prefix))
        files = []
        for blob in blobs:
            if not blob.name.endswith("/"):
                files.append({
                    "File Name": os.path.basename(blob.name),
                    "Size (KB)": round(blob.size / 1024, 2),
                    "Uploaded":  blob.time_created.strftime("%Y-%m-%d %H:%M:%S") if blob.time_created else "N/A",
                    "Full Path": blob.name
                })
        return pd.DataFrame(files)
    except Exception as e:
        return pd.DataFrame()

@st.cache_data(ttl=10)
def get_audit_log():
    try:
        bq = get_bq_client()
        query = f"""
            SELECT FileName, TargetTable, RecordsLoaded, Status, ErrorMessage, LoadDate
            FROM `{PROJECT_ID}.{DATASET_ID}.audit_log`
            ORDER BY LoadDate DESC
            LIMIT 50
        """
        result = bq.query(query).result()
        rows = [dict(row) for row in result]
        return pd.DataFrame(rows)
    except Exception as e:
        return pd.DataFrame()

@st.cache_data(ttl=10)
def get_bq_tables():
    try:
        bq = get_bq_client()
        tables = list(bq.list_tables(f"{PROJECT_ID}.{DATASET_ID}"))
        return [t.table_id for t in tables if t.table_id != "audit_log"]
    except Exception as e:
        return []

@st.cache_data(ttl=10)
def get_table_preview(table_name):
    try:
        bq = get_bq_client()
        query = f"SELECT * FROM `{PROJECT_ID}.{DATASET_ID}.{table_name}` LIMIT 100"
        result = bq.query(query).result()
        rows = [dict(row) for row in result]
        return pd.DataFrame(rows)
    except Exception as e:
        return pd.DataFrame()

@st.cache_data(ttl=10)
def get_table_schema(table_name):
    try:
        bq = get_bq_client()
        table = bq.get_table(f"{PROJECT_ID}.{DATASET_ID}.{table_name}")
        return pd.DataFrame([{
            "Column":      field.name,
            "Type":        field.field_type,
            "Mode":        field.mode,
            "Description": field.description or ""
        } for field in table.schema])
    except Exception as e:
        return pd.DataFrame()

@st.cache_data(ttl=10)
def get_stats():
    try:
        bq = get_bq_client()
        query = f"""
            SELECT
                COUNT(*) as total_loads,
                COUNTIF(Status = 'Success') as successful,
                COUNTIF(Status = 'Failed') as failed,
                SUM(RecordsLoaded) as total_records
            FROM `{PROJECT_ID}.{DATASET_ID}.audit_log`
        """
        result = bq.query(query).result()
        rows = [dict(row) for row in result]
        return pd.DataFrame(rows).iloc[0] if rows else None
    except Exception as e:
        return None

def upload_to_gcs(file_obj, filename, prefix):
    gcs = get_gcs_client()
    bucket = gcs.bucket(BUCKET_NAME)
    blob = bucket.blob(f"{prefix}{filename}")
    blob.upload_from_file(file_obj)
    return f"gs://{BUCKET_NAME}/{prefix}{filename}"

def run_bq_query(query):
    bq = get_bq_client()
    result = bq.query(query).result()
    rows = [dict(row) for row in result]
    return pd.DataFrame(rows)

# =========================================
# STATS ROW
# =========================================
st.subheader("📊 Pipeline Overview")
stats = get_stats()
col1, col2, col3, col4 = st.columns(4)
if stats is not None:
    with col1:
        st.metric("📁 Total Loads", int(stats["total_loads"]))
    with col2:
        st.metric("✅ Successful", int(stats["successful"]))
    with col3:
        st.metric("❌ Failed", int(stats["failed"]))
    with col4:
        st.metric("📝 Total Records", f"{int(stats['total_records']):,}")
else:
    st.info("ℹ️ No data yet — upload a file to get started.")

st.divider()

# =========================================
# TABS
# =========================================
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📤 Upload File",
    "🪣 GCS Bucket",
    "📊 BigQuery Tables",
    "📋 Audit Log",
    "🔍 Query Runner"
])

# =========================================
# TAB 1 — UPLOAD FILE
# =========================================
with tab1:
    st.subheader("📤 Upload File to GCS")
    st.markdown("Upload a file directly to GCS — Cloud Function will auto-process it into BigQuery.")

    col1, col2 = st.columns([2, 1])
    with col1:
        uploaded_file = st.file_uploader(
            "Choose a file",
            type=["csv", "xlsx", "json", "txt"],
            help="Supported: CSV, Excel, JSON, TXT"
        )
    with col2:
        target_folder = st.selectbox(
            "Target GCS Folder",
            ["incoming/", "processed/", "failed/"]
        )

    if uploaded_file:
        st.info(f"📄 **File:** {uploaded_file.name} | **Size:** {round(uploaded_file.size/1024, 2)} KB")

        # Preview file
        st.markdown("**👀 File Preview:**")
        try:
            if uploaded_file.name.endswith(".csv"):
                preview_df = pd.read_csv(uploaded_file)
                uploaded_file.seek(0)
            elif uploaded_file.name.endswith(".xlsx"):
                preview_df = pd.read_excel(uploaded_file)
                uploaded_file.seek(0)
            elif uploaded_file.name.endswith(".json"):
                preview_df = pd.read_json(uploaded_file)
                uploaded_file.seek(0)
            elif uploaded_file.name.endswith(".txt"):
                content = uploaded_file.read().decode("utf-8")
                uploaded_file.seek(0)
                first_line = content.split("\n")[0]
                delim = "\t" if "\t" in first_line else "|" if "|" in first_line else ","
                preview_df = pd.read_csv(io.StringIO(content), delimiter=delim)
                uploaded_file.seek(0)
            st.dataframe(preview_df.head(5), use_container_width=True)
            st.caption(f"Showing 5 of {len(preview_df)} rows — {len(preview_df.columns)} columns")
        except Exception as e:
            st.warning(f"⚠️ Could not preview: {e}")

        if st.button("☁️ Upload to GCS & Process", use_container_width=True, type="primary"):
            with st.status("Processing...", expanded=True) as status:
                st.write("📤 Uploading to GCS...")
                try:
                    gcs_path = upload_to_gcs(uploaded_file, uploaded_file.name, target_folder)
                    st.write(f"✅ Uploaded to: `{gcs_path}`")
                    if target_folder == "incoming/":
                        st.write("⚡ Cloud Function triggered automatically!")
                        st.write("🤖 AI is analyzing metadata...")
                        st.write("📊 Loading data into BigQuery...")
                        time.sleep(3)
                        st.write("✅ Check Audit Log tab in ~30 seconds for results.")
                        status.update(label="✅ Upload complete!", state="complete")
                    else:
                        status.update(label="✅ Upload complete!", state="complete")
                    st.cache_data.clear()
                except Exception as e:
                    status.update(label="❌ Upload failed!", state="error")
                    st.error(f"❌ Error: {e}")

# =========================================
# TAB 2 — GCS BUCKET
# =========================================
with tab2:
    st.subheader("🪣 GCS Bucket Monitor")
    st.markdown(f"Bucket: `gs://{BUCKET_NAME}`")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("#### 📥 Incoming")
        incoming_df = list_gcs_files("incoming/")
        if not incoming_df.empty:
            st.dataframe(
                incoming_df[["File Name", "Size (KB)", "Uploaded"]],
                use_container_width=True,
                hide_index=True
            )
            st.caption(f"{len(incoming_df)} file(s)")
        else:
            st.info("📭 No files in incoming/")

    with col2:
        st.markdown("#### ✅ Processed")
        processed_df = list_gcs_files("processed/")
        if not processed_df.empty:
            st.dataframe(
                processed_df[["File Name", "Size (KB)", "Uploaded"]],
                use_container_width=True,
                hide_index=True
            )
            st.caption(f"{len(processed_df)} file(s)")
        else:
            st.info("📭 No files in processed/")

    with col3:
        st.markdown("#### ❌ Failed")
        failed_df = list_gcs_files("failed/")
        if not failed_df.empty:
            st.dataframe(
                failed_df[["File Name", "Size (KB)", "Uploaded"]],
                use_container_width=True,
                hide_index=True
            )
            st.caption(f"{len(failed_df)} file(s)")
        else:
            st.info("📭 No files in failed/")

# =========================================
# TAB 3 — BIGQUERY TABLES
# =========================================
with tab3:
    st.subheader("📊 BigQuery Tables")

    tables = get_bq_tables()
    if not tables:
        st.info("ℹ️ No data tables found yet. Upload a file to create tables.")
    else:
        st.success(f"✅ {len(tables)} table(s) found in `{DATASET_ID}`")
        selected_table = st.selectbox("Select a table to explore:", tables)

        if selected_table:
            col1, col2 = st.columns([1, 2])

            with col1:
                st.markdown("**📋 Schema:**")
                schema_df = get_table_schema(selected_table)
                if not schema_df.empty:
                    st.dataframe(schema_df, use_container_width=True, hide_index=True)

            with col2:
                st.markdown("**👀 Data Preview (100 rows):**")
                preview_df = get_table_preview(selected_table)
                if not preview_df.empty:
                    st.dataframe(preview_df, use_container_width=True)
                    st.caption(f"{len(preview_df)} rows shown")
                    csv = preview_df.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        label="⬇️ Download as CSV",
                        data=csv,
                        file_name=f"{selected_table}.csv",
                        mime="text/csv"
                    )
                else:
                    st.info("ℹ️ No data in this table yet.")

# =========================================
# TAB 4 — AUDIT LOG
# =========================================
with tab4:
    st.subheader("📋 Audit Log")
    st.markdown("Every file load — success or failure — is recorded here.")

    audit_df = get_audit_log()
    if not audit_df.empty:
        def color_status(val):
            if val == "Success":
                return "background-color: #d4edda; color: #155724"
            elif val == "Failed":
                return "background-color: #f8d7da; color: #721c24"
            return ""

        col1, col2 = st.columns(2)
        with col1:
            status_filter = st.selectbox("Filter by Status:", ["All", "Success", "Failed"])
        with col2:
            table_filter = st.selectbox(
                "Filter by Table:",
                ["All"] + list(audit_df["TargetTable"].unique())
            )

        filtered_df = audit_df.copy()
        if status_filter != "All":
            filtered_df = filtered_df[filtered_df["Status"] == status_filter]
        if table_filter != "All":
            filtered_df = filtered_df[filtered_df["TargetTable"] == table_filter]

        st.dataframe(
            filtered_df.style.map(color_status, subset=["Status"]),
            use_container_width=True,
            hide_index=True
        )
        st.caption(f"Showing {len(filtered_df)} of {len(audit_df)} records")

        csv = filtered_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="⬇️ Download Audit Log",
            data=csv,
            file_name="audit_log.csv",
            mime="text/csv"
        )
    else:
        st.info("ℹ️ No audit records yet. Upload a file to see logs here.")

# =========================================
# TAB 5 — QUERY RUNNER
# =========================================
with tab5:
    st.subheader("🔍 BigQuery Query Runner")
    st.markdown("Run any SQL query against your BigQuery dataset.")

    st.markdown("**⚡ Quick Queries:**")
    qcol1, qcol2, qcol3, qcol4 = st.columns(4)
    with qcol1:
        if st.button("📋 Recent Loads", use_container_width=True):
            st.session_state.query = f"SELECT * FROM `{PROJECT_ID}.{DATASET_ID}.audit_log` ORDER BY LoadDate DESC LIMIT 20"
    with qcol2:
        if st.button("❌ Failed Today", use_container_width=True):
            st.session_state.query = f"SELECT * FROM `{PROJECT_ID}.{DATASET_ID}.audit_log` WHERE Status = 'Failed' AND DATE(LoadDate) = CURRENT_DATE()"
    with qcol3:
        if st.button("📊 Load Summary", use_container_width=True):
            st.session_state.query = f"SELECT TargetTable, COUNT(*) as Loads, SUM(RecordsLoaded) as TotalRecords FROM `{PROJECT_ID}.{DATASET_ID}.audit_log` WHERE Status = 'Success' GROUP BY TargetTable"
    with qcol4:
        if st.button("📈 Daily Trend", use_container_width=True):
            st.session_state.query = f"SELECT DATE(LoadDate) as Date, COUNT(*) as Loads, SUM(RecordsLoaded) as Records FROM `{PROJECT_ID}.{DATASET_ID}.audit_log` GROUP BY Date ORDER BY Date DESC LIMIT 30"

    if "query" not in st.session_state:
        st.session_state.query = f"SELECT * FROM `{PROJECT_ID}.{DATASET_ID}.audit_log` ORDER BY LoadDate DESC LIMIT 10"

    query = st.text_area("SQL Query:", value=st.session_state.query, height=120)

    if st.button("▶️ Run Query", type="primary", use_container_width=True):
        with st.spinner("Running query..."):
            try:
                result_df = run_bq_query(query)
                st.success(f"✅ Query returned {len(result_df)} rows")
                st.dataframe(result_df, use_container_width=True)
                if not result_df.empty:
                    csv = result_df.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        label="⬇️ Download Results",
                        data=csv,
                        file_name="query_results.csv",
                        mime="text/csv"
                    )
            except Exception as e:
                st.error(f"❌ Query error: {e}")

# =========================================
# AUTO REFRESH
# =========================================
if auto_refresh:
    time.sleep(10)
    st.cache_data.clear()
    st.rerun()