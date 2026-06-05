import os
import time
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from google.cloud import storage

CONFIG = {
    "watch_folder":     r"C:\Users\sbajan\Documents\SmartFileAI\uploads\incoming",
    "processed_folder": r"C:\Users\sbajan\Documents\SmartFileAI\uploads\processed",
    "failed_folder":    r"C:\Users\sbajan\Documents\SmartFileAI\uploads\failed",
    "gcs_bucket":       "smartfile-incoming-gcp-filefusion",
    "gcs_prefix":       "incoming/",
    "supported_ext":    [".csv", ".xlsx", ".json", ".txt", ".xml", ".parquet"],
    "process_delay":    3
}

def get_timestamped_name(filename):
    name, ext = os.path.splitext(filename)
    timestamp = datetime.now().strftime("%Y-%m-%d.%H.%M.%S")
    return f"{name}_{timestamp}{ext}"

def move_file(src, dest_folder, filename):
    os.makedirs(dest_folder, exist_ok=True)
    timestamped_name = get_timestamped_name(filename)
    dest = os.path.join(dest_folder, timestamped_name)
    try:
        os.rename(src, dest)
        print(f"Moved to: {dest_folder}\\{timestamped_name}")
    except Exception as e:
        print(f"Could not move file: {e}")

def upload_to_gcs(file_path):
    filename = os.path.basename(file_path)
    client = storage.Client()
    bucket = client.bucket(CONFIG["gcs_bucket"])
    blob = bucket.blob(CONFIG["gcs_prefix"] + filename)
    blob.upload_from_filename(file_path)
    print(f"Uploaded to GCS: gs://{CONFIG['gcs_bucket']}/{CONFIG['gcs_prefix']}{filename}")
    return True

def process_file(file_path):
    filename = os.path.basename(file_path)
    print(f"Detected: {filename}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        time.sleep(CONFIG["process_delay"])
        upload_to_gcs(file_path)
        move_file(file_path, CONFIG["processed_folder"], filename)
        print(f"Done! Cloud Function will process it in GCP.")
    except Exception as e:
        print(f"Upload failed: {e}")
        move_file(file_path, CONFIG["failed_folder"], filename)

class FileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext in CONFIG["supported_ext"]:
                process_file(event.src_path)

if __name__ == "__main__":
    os.makedirs(CONFIG["watch_folder"], exist_ok=True)
    print("SmartFile GCS Watcher Started")
    print(f"Watching: {CONFIG['watch_folder']}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Drop any file to process...")

    observer = Observer()
    observer.schedule(FileHandler(), CONFIG["watch_folder"], recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("Watcher stopped.")
    observer.join()
