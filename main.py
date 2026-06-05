# main.py — Enhanced Cloud Function with all 7 improvements

import os
import json
import hashlib
import pandas as pd
import functions_framework
import xml.etree.ElementTree as ET
from io import BytesIO
from datetime import datetime
from google.cloud import storage, bigquery
from openai import OpenAI

# =========================================
# CONFIG
# =========================================
PROJECT_ID     = "gcp-filefusion"
DATASET_ID     = "smartfile_data"
AUDIT_TABLE    = "audit_log"
VALIDATION_TABLE = "validation_log"
DUPLICATE_TABLE  = "duplicate_log"
GCS_BUCKET     = "smartfile-incoming-gcp-filefusion"
OPENROUTER_KEY = os.environ.get("")
SUPPORTED_EXT  = [".csv", ".xlsx", ".xls", ".json", ".txt", ".parquet", ".xml"]

# =========================================
# AI CLIENT
# =========================================
def get_ai_client():
    return OpenAI(api_key=OPENROUTER_KEY, base_url="https://openrouter.ai/api/v1")

# =========================================
# READ FILE FROM GCS — ALL FORMATS
# =========================================
def read_file_from_gcs(bucket_name, blob_name):
    gcs_client = storage.Client()
    bucket = gcs_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    content = blob.download_as_bytes()
    filename = os.path.basename(blob_name).lower()

    if filename.endswith(".csv"):
        df = pd.read_csv(BytesIO(content))
    elif filename.endswith(".xlsx") or filename.endswith(".xls"):
        df = pd.read_excel(BytesIO(content))
    elif filename.endswith(".json"):
        try:
            df = pd.read_json(BytesIO(content))
        except Exception:
            data = json.loads(content.decode("utf-8"))
            if isinstance(data, list):
                df = pd.DataFrame(data)
            elif isinstance(data, dict):
                df = pd.DataFrame([data])
    elif filename.endswith(".txt"):
        text = content.decode("utf-8")
        first_line = text.split("\n")[0]
        delim = "\t" if "\t" in first_line else "|" if "|" in first_line else ","
        df = pd.read_csv(BytesIO(content), delimiter=delim)
    elif filename.endswith(".parquet"):
        df = pd.read_parquet(BytesIO(content))
    elif filename.endswith(".xml"):
        tree = ET.parse(BytesIO(content))
        root = tree.getroot()
        rows = []
        for child in root:
            row = {elem.tag: elem.text for elem in child}
            if row:
                rows.append(row)
        if not rows:
            rows = [child.attrib for child in root]
        df = pd.DataFrame(rows)
    else:
        raise ValueError(f"Unsupported file type: {filename}")

    return df, os.path.basename(blob_name), content

# =========================================
# COMPUTE CHECKSUM
# =========================================
def compute_checksum(content):
    return hashlib.md5(content).hexdigest()

# =========================================
# VALIDATE FILE
# =========================================
def validate_file(df, filename):
    errors = []
    warnings = []

    # Check 1 — Empty file
    if df is None or len(df) == 0:
        errors.append("File has no data rows")
        return False, errors, warnings, {}

    # Check 2 — Missing headers
    unnamed = [c for c in df.columns if str(c).startswith("Unnamed")]
    if unnamed:
        errors.append(f"Missing column headers detected: {unnamed}")

    # Check 3 — All columns empty
    empty_cols = [c for c in df.columns if df[c].isna().all()]
    if empty_cols:
        warnings.append(f"Columns with all empty values: {empty_cols}")

    # Check 4 — Duplicate rows
    dup_count = df.duplicated().sum()
    if dup_count > 0:
        warnings.append(f"Duplicate rows detected: {dup_count}")

    # Check 5 — Corrupt data (all NaN rows)
    null_rows = df.isna().all(axis=1).sum()
    if null_rows > 0:
        warnings.append(f"Completely empty rows: {null_rows}")

    # Check 6 — Row count validation
    source_rows = len(df)
    valid_rows  = len(df.dropna(how="all"))
    invalid_rows = source_rows - valid_rows

    stats = {
        "source_rows":   source_rows,
        "valid_rows":    valid_rows,
        "invalid_rows":  invalid_rows,
        "duplicate_rows": int(dup_count),
        "empty_cols":    len(empty_cols),
        "columns":       len(df.columns),
        "column_names":  ", ".join(df.columns.tolist()[:20])
    }

    # Check 7 — If more than 50% rows are invalid, stop
    if source_rows > 0 and (invalid_rows / source_rows) > 0.5:
        errors.append(f"More than 50% rows are invalid ({invalid_rows}/{source_rows}). File rejected.")

    is_valid = len(errors) == 0
    return is_valid, errors, warnings, stats

# =========================================
# CHECK DUPLICATE FILE
# =========================================
def is_duplicate(bq, filename, row_count, checksum):
    try:
        ensure_dataset(bq)
        query = f"""
            SELECT COUNT(*) as cnt
            FROM `{PROJECT_ID}.{DATASET_ID}.{AUDIT_TABLE}`
            WHERE FileName = '{filename}'
            AND RecordsLoaded = {row_count}
            AND FileChecksum = '{checksum}'
            AND Status = 'Success'
        """
        result = list(bq.query(query).result())
        return result[0]["cnt"] > 0
    except Exception:
        return False

# =========================================
# AI METADATA ANALYSIS
# =========================================
def analyze_metadata(df, filename):
    client = get_ai_client()
    sample = df.head(5).to_string(index=False)
    columns = df.dtypes.reset_index()
    columns.columns = ["Column", "Dtype"]
    col_info = columns.to_string(index=False)

    prompt = f"""
You are a data engineering expert working with Google BigQuery.
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
      "bq_type": "STRING",
      "description": "what this column means"
    }}
  ],
  "file_summary": "one sentence about what this file contains"
}}

BigQuery types to use: STRING, INTEGER, FLOAT, BOOLEAN, DATE, TIMESTAMP, NUMERIC
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
# ENSURE DATASET
# =========================================
def ensure_dataset(bq):
    dataset_ref = f"{PROJECT_ID}.{DATASET_ID}"
    try:
        bq.get_dataset(dataset_ref)
    except Exception:
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = "asia-south1"
        bq.create_dataset(dataset)
        print(f"Dataset created: {DATASET_ID}")

# =========================================
# LOAD TO BIGQUERY
# =========================================
def load_to_bigquery(df, metadata):
    bq = bigquery.Client(project=PROJECT_ID)
    ensure_dataset(bq)

    table_name = metadata["suggested_table_name"]
    table_ref  = f"{PROJECT_ID}.{DATASET_ID}.{table_name}"

    schema = [
        bigquery.SchemaField(col["name"], "STRING")
        for col in metadata["columns"]
    ]

    ai_cols = [c["name"] for c in metadata["columns"]]
    if len(ai_cols) == len(df.columns):
        df.columns = ai_cols

    df = df.where(pd.notnull(df), None)
    for col in df.columns:
        if df[col].dtype == "datetime64[ns]":
            df[col] = df[col].astype(str).replace("NaT", None)
        df[col] = df[col].astype(str).replace("nan", None).replace("None", None)

    json_rows = df.to_dict(orient="records")

    try:
        job_config = bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
            autodetect=False
        )
        job = bq.load_table_from_json(json_rows, table_ref, job_config=job_config)
        job.result()
    except Exception as e:
        if "Schema does not match" in str(e) or "has changed type" in str(e):
            print(f"Schema mismatch — truncating and reloading...")
            job_config = bigquery.LoadJobConfig(
                schema=schema,
                write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
                create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
                autodetect=False
            )
            job = bq.load_table_from_json(json_rows, table_ref, job_config=job_config)
            job.result()
        else:
            raise e

    print(f"Loaded {len(df)} rows into BigQuery: {table_ref}")
    return table_name

# =========================================
# AUDIT LOG
# =========================================
def log_audit(filename, table_name, records, status,
              checksum=None, source_rows=0, valid_rows=0,
              invalid_rows=0, load_time_sec=0, error=None):
    bq = bigquery.Client(project=PROJECT_ID)
    ensure_dataset(bq)

    audit_ref = f"{PROJECT_ID}.{DATASET_ID}.{AUDIT_TABLE}"
    audit_schema = [
        bigquery.SchemaField("FileName",       "STRING"),
        bigquery.SchemaField("TargetTable",    "STRING"),
        bigquery.SchemaField("RecordsLoaded",  "INTEGER"),
        bigquery.SchemaField("Status",         "STRING"),
        bigquery.SchemaField("ErrorMessage",   "STRING"),
        bigquery.SchemaField("FileChecksum",   "STRING"),
        bigquery.SchemaField("SourceRows",     "INTEGER"),
        bigquery.SchemaField("ValidRows",      "INTEGER"),
        bigquery.SchemaField("InvalidRows",    "INTEGER"),
        bigquery.SchemaField("LoadTimeSec",    "FLOAT"),
        bigquery.SchemaField("LoadDate",       "TIMESTAMP"),
    ]

    rows = [{
        "FileName":      filename,
        "TargetTable":   table_name,
        "RecordsLoaded": records,
        "Status":        status,
        "ErrorMessage":  str(error)[:2000] if error else None,
        "FileChecksum":  checksum,
        "SourceRows":    source_rows,
        "ValidRows":     valid_rows,
        "InvalidRows":   invalid_rows,
        "LoadTimeSec":   load_time_sec,
        "LoadDate":      datetime.utcnow().isoformat()
    }]

    job_config = bigquery.LoadJobConfig(
        schema=audit_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED
    )
    job = bq.load_table_from_json(rows, audit_ref, job_config=job_config)
    job.result()
    print("Audit log updated.")

# =========================================
# VALIDATION LOG
# =========================================
def log_validation(filename, table_name, stats, errors, warnings):
    bq = bigquery.Client(project=PROJECT_ID)
    ensure_dataset(bq)

    val_ref = f"{PROJECT_ID}.{DATASET_ID}.{VALIDATION_TABLE}"
    val_schema = [
        bigquery.SchemaField("FileName",      "STRING"),
        bigquery.SchemaField("TargetTable",   "STRING"),
        bigquery.SchemaField("SourceRows",    "INTEGER"),
        bigquery.SchemaField("ValidRows",     "INTEGER"),
        bigquery.SchemaField("InvalidRows",   "INTEGER"),
        bigquery.SchemaField("DuplicateRows", "INTEGER"),
        bigquery.SchemaField("ColumnCount",   "INTEGER"),
        bigquery.SchemaField("ColumnNames",   "STRING"),
        bigquery.SchemaField("Errors",        "STRING"),
        bigquery.SchemaField("Warnings",      "STRING"),
        bigquery.SchemaField("ValidatedAt",   "TIMESTAMP"),
    ]

    rows = [{
        "FileName":      filename,
        "TargetTable":   table_name,
        "SourceRows":    stats.get("source_rows", 0),
        "ValidRows":     stats.get("valid_rows", 0),
        "InvalidRows":   stats.get("invalid_rows", 0),
        "DuplicateRows": stats.get("duplicate_rows", 0),
        "ColumnCount":   stats.get("columns", 0),
        "ColumnNames":   stats.get("column_names", ""),
        "Errors":        " | ".join(errors) if errors else None,
        "Warnings":      " | ".join(warnings) if warnings else None,
        "ValidatedAt":   datetime.utcnow().isoformat()
    }]

    job_config = bigquery.LoadJobConfig(
        schema=val_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED
    )
    job = bq.load_table_from_json(rows, val_ref, job_config=job_config)
    job.result()
    print("Validation log updated.")

# =========================================
# DUPLICATE LOG
# =========================================
def log_duplicate(filename, checksum, row_count):
    bq = bigquery.Client(project=PROJECT_ID)
    ensure_dataset(bq)

    dup_ref = f"{PROJECT_ID}.{DATASET_ID}.{DUPLICATE_TABLE}"
    dup_schema = [
        bigquery.SchemaField("FileName",    "STRING"),
        bigquery.SchemaField("FileChecksum","STRING"),
        bigquery.SchemaField("RowCount",    "INTEGER"),
        bigquery.SchemaField("DetectedAt",  "TIMESTAMP"),
    ]

    rows = [{
        "FileName":     filename,
        "FileChecksum": checksum,
        "RowCount":     row_count,
        "DetectedAt":   datetime.utcnow().isoformat()
    }]

    job_config = bigquery.LoadJobConfig(
        schema=dup_schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED
    )
    job = bq.load_table_from_json(rows, dup_ref, job_config=job_config)
    job.result()
    print("Duplicate log updated.")

# =========================================
# MOVE FILE IN GCS
# =========================================
def move_gcs_file(bucket_name, source_blob, destination_prefix):
    gcs_client = storage.Client()
    bucket = gcs_client.bucket(bucket_name)
    filename = os.path.basename(source_blob)
    source = bucket.blob(source_blob)
    bucket.copy_blob(source, bucket, f"{destination_prefix}{filename}")
    source.delete()
    print(f"Moved to gs://{bucket_name}/{destination_prefix}{filename}")

# =========================================
# CORE PROCESS FILE
# =========================================
def process_single_file(bucket_name, blob_name):
    filename   = os.path.basename(blob_name)
    table_name = "unknown"
    start_time = datetime.utcnow()

    try:
        print(f"Processing: {filename}")

        # Step 1 — Read file
        df, fname, content = read_file_from_gcs(bucket_name, blob_name)
        print(f"Read: {len(df)} rows, {len(df.columns)} columns")

        # Step 2 — Compute checksum
        checksum  = compute_checksum(content)
        row_count = len(df)
        print(f"Checksum: {checksum}")

        # Step 3 — Duplicate check
        bq = bigquery.Client(project=PROJECT_ID)
        ensure_dataset(bq)
        if is_duplicate(bq, filename, row_count, checksum):
            print(f"DUPLICATE DETECTED: {filename} already loaded with same content.")
            log_duplicate(filename, checksum, row_count)
            move_gcs_file(bucket_name, blob_name, "duplicate/")
            return False, table_name, 0, "DUPLICATE"

        # Step 4 — Validate file
        is_valid, errors, warnings, stats = validate_file(df, filename)
        print(f"Validation: valid={is_valid}, errors={errors}, warnings={warnings}")

        if not is_valid:
            print(f"Validation failed: {errors}")
            load_time = (datetime.utcnow() - start_time).total_seconds()
            log_audit(filename, table_name, 0, "Failed",
                      checksum=checksum,
                      source_rows=stats.get("source_rows", 0),
                      valid_rows=stats.get("valid_rows", 0),
                      invalid_rows=stats.get("invalid_rows", 0),
                      load_time_sec=load_time,
                      error=" | ".join(errors))
            log_validation(filename, table_name, stats, errors, warnings)
            move_gcs_file(bucket_name, blob_name, "failed/")
            return False, table_name, 0, "VALIDATION_FAILED"

        # Step 5 — AI metadata analysis
        metadata   = analyze_metadata(df, filename)
        table_name = metadata["suggested_table_name"]
        print(f"Table: {table_name} — {metadata['file_summary']}")

        # Step 6 — Log validation results
        log_validation(filename, table_name, stats, errors, warnings)

        # Step 7 — Load to BigQuery
        load_to_bigquery(df, metadata)

        # Step 8 — Compute load time
        load_time = (datetime.utcnow() - start_time).total_seconds()

        # Step 9 — Audit log
        log_audit(filename, table_name, row_count, "Success",
                  checksum=checksum,
                  source_rows=stats.get("source_rows", 0),
                  valid_rows=stats.get("valid_rows", 0),
                  invalid_rows=stats.get("invalid_rows", 0),
                  load_time_sec=load_time)

        # Step 10 — Move to processed
        move_gcs_file(bucket_name, blob_name, "processed/")
        print(f"Done: {filename} in {load_time:.1f}s")
        return True, table_name, row_count, "SUCCESS"

    except Exception as e:
        print(f"Error: {e}")
        load_time = (datetime.utcnow() - start_time).total_seconds()
        log_audit(filename, table_name, 0, "Failed",
                  load_time_sec=load_time, error=e)
        try:
            move_gcs_file(bucket_name, blob_name, "failed/")
        except Exception as me:
            print(f"Move error: {me}")
        return False, table_name, 0, "ERROR"

# =========================================
# GCS TRIGGER
# =========================================
@functions_framework.cloud_event
def on_file_upload(cloud_event):
    data        = cloud_event.data
    bucket_name = data["bucket"]
    blob_name   = data["name"]
    filename    = os.path.basename(blob_name)

    if not blob_name.startswith("incoming/"):
        print(f"Skipping {blob_name} - not in incoming/")
        return

    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED_EXT:
        print(f"Skipping {filename} - unsupported extension")
        return

    process_single_file(bucket_name, blob_name)

# =========================================
# HTTP TRIGGER — Cloud Scheduler
# =========================================
@functions_framework.http
def scheduled_check(request):
    print(f"Scheduled run: {datetime.utcnow().isoformat()}")
    gcs_client = storage.Client()
    bucket     = gcs_client.bucket(GCS_BUCKET)
    blobs      = list(bucket.list_blobs(prefix="incoming/"))

    total = success = failed = duplicates = 0
    results = []

    for blob in blobs:
        if blob.name.endswith("/"):
            continue
        ext = os.path.splitext(blob.name)[1].lower()
        if ext not in SUPPORTED_EXT:
            continue

        total += 1
        ok, table_name, records, status = process_single_file(GCS_BUCKET, blob.name)

        if ok:
            success += 1
            results.append(f"SUCCESS: {os.path.basename(blob.name)} -> {table_name} ({records} rows)")
        elif status == "DUPLICATE":
            duplicates += 1
            results.append(f"DUPLICATE: {os.path.basename(blob.name)} already loaded")
        else:
            failed += 1
            results.append(f"FAILED: {os.path.basename(blob.name)}")

    summary = "\n".join(results) if results else "No files in incoming/ folder."
    response = f"Scheduled Run: {datetime.utcnow().isoformat()}\nTotal:{total} Success:{success} Failed:{failed} Duplicates:{duplicates}\n{summary}"
    print(response)
    return response, 200