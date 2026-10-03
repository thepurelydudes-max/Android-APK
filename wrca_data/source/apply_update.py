from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import urllib.request
from pathlib import Path


def safe_rel(value: str) -> Path:
    path = Path(str(value or "").replace("\\", "/"))
    if not str(path) or path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Unsafe relative path: {value!r}")
    return path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", default="wild_rift/assets")
    args = parser.parse_args()

    source_dir = Path(__file__).resolve().parent
    assets = Path(args.assets_root).resolve()
    database = assets / "data" / "wildrift.db"
    cache = assets / "cache"
    if not database.is_file():
        raise SystemExit(f"Missing database: {database}")
    cache.mkdir(parents=True, exist_ok=True)

    sql_path = source_dir / "update.sql"
    if sql_path.is_file():
        sql = sql_path.read_text(encoding="utf-8").strip()
        if sql and not sql.startswith("-- no data changes"):
            with sqlite3.connect(database) as con:
                con.executescript(sql)
                con.commit()

    support_path = source_dir / "support.json"
    if support_path.is_file():
        support = json.loads(support_path.read_text(encoding="utf-8"))
        if not isinstance(support, dict):
            raise RuntimeError("support.json must contain a JSON object")
        if not isinstance(support.get("enabled", True), bool):
            raise RuntimeError("support.json enabled must be boolean")
        for key in (
            "title_ru", "title_en", "body_ru", "body_en",
            "method_ru", "method_en", "details", "recipient",
        ):
            if key in support and not isinstance(support.get(key), str):
                raise RuntimeError(f"support.json {key} must be a string")
        payload = json.dumps(support, ensure_ascii=False, separators=(",", ":"))
        with sqlite3.connect(database) as con:
            con.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("support_config_json", payload),
            )
            con.commit()

    delete_path = source_dir / "delete_cache.txt"
    if delete_path.is_file():
        for raw in delete_path.read_text(encoding="utf-8").splitlines():
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            target = cache / safe_rel(value)
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)

    overrides = source_dir / "cache_overrides"
    if overrides.is_dir():
        for src in sorted(x for x in overrides.rglob("*") if x.is_file()):
            rel = src.relative_to(overrides)
            dst = cache / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    downloads_path = source_dir / "download_cache.json"
    if downloads_path.is_file():
        rows = json.loads(downloads_path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise RuntimeError("download_cache.json must contain a JSON list")
        for row in rows:
            if not isinstance(row, dict):
                raise RuntimeError("download_cache.json rows must be objects")
            url = str(row.get("url") or "").strip()
            rel = safe_rel(str(row.get("path") or ""))
            expected = str(row.get("sha256") or "").strip().lower()
            if not url.startswith("https://"):
                raise RuntimeError(f"Only HTTPS downloads are allowed: {url}")
            if len(expected) != 64:
                raise RuntimeError(f"Missing SHA-256 for {rel}")
            dst = cache / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_suffix(dst.suffix + ".download")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "WRCA-GitHub-Package-Builder/1"},
            )
            with urllib.request.urlopen(req, timeout=60) as response, tmp.open("wb") as fh:
                shutil.copyfileobj(response, fh)
            actual = sha256(tmp)
            if actual != expected:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Downloaded cache SHA mismatch for {rel}: {actual} != {expected}"
                )
            tmp.replace(dst)

    with sqlite3.connect(database) as con:
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        champions = con.execute("SELECT COUNT(*) FROM champions").fetchone()[0]
        patch = dict(con.execute("SELECT key,value FROM meta")).get("patch_version", "")
    if str(quick).casefold() != "ok":
        raise RuntimeError("SQLite quick_check failed after source patch")

    print(
        f"WRCA source patch applied: patch={patch or '-'}, champions={champions}, "
        f"database={database}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
