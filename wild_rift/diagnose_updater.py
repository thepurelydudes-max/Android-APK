from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import traceback
from pathlib import Path

runtime = Path(tempfile.mkdtemp(prefix="wrca-live-update-"))
os.environ["FLET_APP_STORAGE_DATA"] = str(runtime)

import paths
paths.ensure_initial_data()
import db
import updater

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def progress(message: str) -> None:
    print("PROGRESS:", message, flush=True)

db_path = Path(db.DB_PATH)
before_hash = sha256(db_path)
print("RUNTIME:", runtime, flush=True)
print("DB_BEFORE:", before_hash, flush=True)
print("BASELINE_COUNTS:", json.dumps(updater._database_counts(db_path), ensure_ascii=False), flush=True)

exit_code = 0
try:
    summary = updater.update_all(progress=progress, lang="ru")
    print("SUMMARY:", json.dumps(summary, ensure_ascii=False, default=str, indent=2), flush=True)
except Exception as exc:
    exit_code = 2
    print("UPDATE_EXCEPTION:", type(exc).__name__, str(exc), flush=True)
    traceback.print_exc()

after_hash = sha256(db_path)
print("DB_AFTER:", after_hash, flush=True)
print("LIVE_DB_CHANGED:", before_hash != after_hash, flush=True)
print("FINAL_COUNTS:", json.dumps(updater._database_counts(db_path), ensure_ascii=False), flush=True)

# On failed validation the live DB must be byte-identical.
if exit_code and before_hash != after_hash:
    print("ATOMICITY_BROKEN: failed update modified the live database", flush=True)
    raise SystemExit(3)
raise SystemExit(exit_code)
