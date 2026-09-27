from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import db
import updater
from paths import ASSETS_DIR, RUNTIME_DIR, ensure_initial_data, resolve_media_path


def _valid_image(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def _audit() -> dict:
    with db.connect() as con:
        champions = [
            dict(row)
            for row in con.execute(
                "SELECT id,name,icon_path,icon_url FROM champions ORDER BY id"
            ).fetchall()
        ]
        items = [
            dict(row)
            for row in con.execute(
                "SELECT name,tier,icon_path,icon_url FROM items "
                "WHERE lower(tier)='upgraded' ORDER BY name"
            ).fetchall()
        ]

    missing_champions = []
    for row in champions:
        icon_path = str(row.get("icon_path") or "").strip()
        if not icon_path or not _valid_image(resolve_media_path(icon_path)):
            missing_champions.append(str(row.get("id") or row.get("name") or "?"))

    missing_items = []
    for row in items:
        icon_path = str(row.get("icon_path") or "").strip()
        if not icon_path or not _valid_image(resolve_media_path(icon_path)):
            missing_items.append(str(row.get("name") or "?"))

    return {
        "champions_total": len(champions),
        "champions_cached": len(champions) - len(missing_champions),
        "champions_missing": missing_champions,
        "items_total": len(items),
        "items_cached": len(items) - len(missing_items),
        "items_missing": missing_items,
    }


def _copy_runtime_into_bundle() -> None:
    runtime_db = RUNTIME_DIR / "data" / "wildrift.db"
    bundled_db = ASSETS_DIR / "data" / "wildrift.db"
    runtime_cache = RUNTIME_DIR / "cache"
    bundled_cache = ASSETS_DIR / "cache"

    if not runtime_db.is_file():
        raise FileNotFoundError(f"Updated runtime database is missing: {runtime_db}")
    if not runtime_cache.is_dir():
        raise FileNotFoundError(f"Updated runtime cache is missing: {runtime_cache}")

    bundled_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(runtime_db, bundled_db)

    # Runtime starts as a merge of the previous bundled cache, then updater adds
    # or replaces verified files. Copy that complete last-known-good tree into
    # assets so a clean Android install needs no first-run media download.
    if bundled_cache.exists():
        shutil.rmtree(bundled_cache)
    shutil.copytree(runtime_cache, bundled_cache)


def main() -> int:
    print(f"Bundled assets: {ASSETS_DIR}", flush=True)
    print(f"Refresh runtime: {RUNTIME_DIR}", flush=True)

    # Start from the repository's last-known-good bundled DB/cache. All updater
    # source replacements are already non-destructive when a live source fails.
    ensure_initial_data()
    db.init_db()

    progress_messages: list[str] = []

    def progress(message: str) -> None:
        value = str(message or "").strip()
        if value:
            progress_messages.append(value)
            print(value, flush=True)

    summary = updater.update_all(progress=progress, lang="ru")
    audit = _audit()

    _copy_runtime_into_bundle()

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "github_sha": os.environ.get("GITHUB_SHA", ""),
        "patch": summary.get("patch", ""),
        "last_update": summary.get("last_update", ""),
        "audit": audit,
        "warnings": [str(value) for value in summary.get("errors", [])],
    }
    manifest_path = ASSETS_DIR / "data" / "bundled_seed_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("\nBundled seed audit", flush=True)
    print(
        f"Champions: {audit['champions_cached']}/{audit['champions_total']} cached",
        flush=True,
    )
    if audit["champions_missing"]:
        print(
            "Missing champion portraits: " + ", ".join(audit["champions_missing"]),
            flush=True,
        )
    print(
        f"Items: {audit['items_cached']}/{audit['items_total']} cached",
        flush=True,
    )
    if audit["items_missing"]:
        print(
            "Missing item icons: " + ", ".join(audit["items_missing"]),
            flush=True,
        )

    warnings = manifest["warnings"]
    if warnings:
        print(f"Updater completed with {len(warnings)} warning block(s):", flush=True)
        for warning in warnings:
            print(warning, flush=True)
    else:
        print("Updater completed without warnings.", flush=True)

    # A source outage is allowed because the updater retains the bundled
    # last-known-good rows/files. Packaging itself must still be internally sane.
    if audit["items_missing"]:
        raise RuntimeError(
            "Refusing to package a seed with missing final-item icons: "
            + ", ".join(audit["items_missing"])
        )
    if audit["champions_cached"] == 0:
        raise RuntimeError("Refusing to package a seed with no champion portraits.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
