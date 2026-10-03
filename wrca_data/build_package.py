from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL_VERSION = 1
DEFAULT_MIN_APP_VERSION = "3.9.0"
REPO = "thepurelydudes-max/Android-APK"

def load_package_source_metadata() -> dict:
    path = Path(__file__).resolve().parent / "source" / "package_metadata.json"
    if not path.is_file():
        raise SystemExit(f"Missing package metadata file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Invalid package metadata JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("package_metadata.json must contain a JSON object")
    patch = str(value.get("patch") or "").strip()
    if not patch:
        raise SystemExit("package_metadata.json must define a non-empty patch")
    return value

def write_package_db_metadata(
    database: Path,
    *,
    package_version: str,
    patch: str,
    generated_at: datetime,
) -> None:
    display_time = generated_at.astimezone(timezone.utc).strftime("%d-%m-%Y %H:%M")
    iso_time = generated_at.astimezone(timezone.utc).isoformat()
    rows = {
        "patch_version": patch,
        "tier_patch": patch,
        "item_dataset_checked_patch": patch,
        "last_update": display_time,
        "last_update_iso": iso_time,
        "package_version": package_version,
        "package_generated_at": iso_time,
        "package_protocol_version": str(PROTOCOL_VERSION),
    }
    with sqlite3.connect(database) as con:
        con.executemany(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            rows.items(),
        )
        con.commit()
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or str(quick[0]).casefold() != "ok":
            raise SystemExit("SQLite quick_check failed after package metadata update")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha256(folder: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(x for x in folder.rglob("*") if x.is_file()):
        rel = path.relative_to(folder).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()

def db_audit(path: Path) -> tuple[dict[str, int], dict[str, str]]:
    with sqlite3.connect(path) as con:
        quick = con.execute("PRAGMA quick_check").fetchone()
        if not quick or str(quick[0]).casefold() != "ok":
            raise SystemExit("SQLite quick_check failed")
        tables = {str(row[0]) for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = (
            "champions", "stats", "champion_tiers", "matchups", "items",
            "item_pools", "role_builds", "role_build_variants",
            "counter_items", "build_page_cache", "matchup_page_cache",
            "runes", "role_runes", "role_rune_variants",
            "role_rune_adaptations",
        )
        missing = [name for name in required if name not in tables]
        if missing:
            raise SystemExit("Missing DB tables: " + ", ".join(missing))
        counts = {
            table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in required
        }
        counts["role_build_champions"] = int(
            con.execute("SELECT COUNT(DISTINCT champion_id) FROM role_builds").fetchone()[0]
        )
        counts["build_cache_champions"] = int(
            con.execute("SELECT COUNT(DISTINCT champion_id) FROM build_page_cache").fetchone()[0]
        )
        counts["matchup_cache_champions"] = int(
            con.execute("SELECT COUNT(DISTINCT champion_id) FROM matchup_page_cache").fetchone()[0]
        )
        counts["tier_roles"] = int(
            con.execute("SELECT COUNT(DISTINCT role) FROM champion_tiers").fetchone()[0]
        )
        counts["rune_role_pairs"] = int(
            con.execute(
                "SELECT COUNT(*) FROM (SELECT DISTINCT champion_id,role FROM role_runes)"
            ).fetchone()[0]
        )
        counts["localized_runes"] = int(
            con.execute(
                "SELECT COUNT(*) FROM runes "
                "WHERE TRIM(COALESCE(name_ru,''))<>'' "
                "AND TRIM(COALESCE(effect_ru,''))<>''"
            ).fetchone()[0]
        )
        meta = {str(k): str(v) for k, v in con.execute("SELECT key,value FROM meta")}
        return counts, meta

def validate_complete(
    counts: dict[str, int],
    champ_icons: int,
    item_icons: int,
    rune_icons: int,
) -> None:
    champions = counts["champions"]
    checks = {
        "champions": champions >= 100,
        "stats": counts["stats"] > 0,
        "champion_tiers": counts["champion_tiers"] > 0,
        "tier_roles": counts["tier_roles"] >= 5,
        "matchups": counts["matchups"] > 0,
        "items": counts["items"] > 0,
        "item_pools": counts["item_pools"] > 0,
        "role_builds": counts["role_builds"] > 0,
        "role_build_variants": counts["role_build_variants"] > 0,
        "counter_items": counts["counter_items"] > 0,
        "role_build_champions": counts["role_build_champions"] == champions,
        "build_cache_champions": counts["build_cache_champions"] == champions,
        "matchup_cache_champions": counts["matchup_cache_champions"] == champions,
        "runes": counts["runes"] >= 51,
        "role_runes": counts["role_runes"] >= 226,
        "rune_role_pairs": counts["rune_role_pairs"] >= 226,
        "role_rune_variants": counts["role_rune_variants"] > 0,
        "localized_runes": counts["localized_runes"] >= 51,
        "champion_icons": champ_icons == champions,
        "item_icons": item_icons >= counts["items"],
        "rune_icons": rune_icons > 0,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        details = ", ".join(f"{key}={counts.get(key, '-')}" for key in failed)
        raise SystemExit("Package completeness checks failed: " + details)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", default="wild_rift/assets")
    parser.add_argument("--output", default="dist/wrca-data")
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--min-app-version", default=DEFAULT_MIN_APP_VERSION)
    args = parser.parse_args()

    assets = Path(args.assets_root).resolve()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    db_path = assets / "data" / "wildrift.db"
    cache = assets / "cache"
    if not db_path.is_file() or not cache.is_dir():
        raise SystemExit("Expected wildrift.db and cache/ in assets root")

    source_meta = load_package_source_metadata()
    generated_at = datetime.now(timezone.utc)
    patch = str(source_meta["patch"]).strip()
    write_package_db_metadata(
        db_path,
        package_version=args.package_version,
        patch=patch,
        generated_at=generated_at,
    )

    counts, meta = db_audit(db_path)
    champ_icons = len(list((cache / "champions").glob("*.png")))
    item_icons = len(list((cache / "items").glob("*.png")))
    rune_icons = len([p for p in (cache / "runes").glob("*") if p.is_file()])
    validate_complete(counts, champ_icons, item_icons, rune_icons)
    counts["champion_icons"] = champ_icons
    counts["item_icons"] = item_icons
    counts["rune_icons"] = rune_icons

    db_patch = meta.get("patch_version", "").strip()
    if db_patch != patch:
        raise SystemExit(f"DB patch metadata mismatch: {db_patch!r} != {patch!r}")
    if meta.get("package_version", "").strip() != args.package_version:
        raise SystemExit("DB package_version metadata mismatch")

    internal = {
        "protocol_version": PROTOCOL_VERSION,
        "package_version": args.package_version,
        "min_app_version": args.min_app_version,
        "patch": patch,
        "generated_at_utc": generated_at.isoformat(),
        "source_revision": os.environ.get("GITHUB_SHA", ""),
        "database_sha256": sha256(db_path),
        "cache_sha256": tree_sha256(cache),
        "counts": counts,
    }
    internal_path = out / "manifest.json"
    internal_path.write_text(
        json.dumps(internal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    zip_name = f"WRCA-data-{args.package_version}.zip"
    zip_path = out / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(internal_path, "manifest.json")
        zf.write(db_path, "data/wildrift.db")
        # Package the complete cache tree, not only PNG folders. SQLite
        # media metadata/auxiliary cache files must travel with the exact cache
        # tree whose SHA-256 is recorded in manifest.json.
        for path in sorted(cache.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(assets).as_posix())

    package_sha = sha256(zip_path)
    manifest_sha = sha256(internal_path)
    size_bytes = zip_path.stat().st_size
    download_url = (
        f"https://github.com/{REPO}/releases/download/"
        f"{args.release_tag}/{zip_name}"
    )
    latest = {
        "protocol_version": PROTOCOL_VERSION,
        "package_version": args.package_version,
        "min_app_version": args.min_app_version,
        "patch": patch,
        "generated_at_utc": internal["generated_at_utc"],
        "release_tag": args.release_tag,
        "download_url": download_url,
        "size_bytes": size_bytes,
        "sha256": package_sha,
        "manifest_sha256": manifest_sha,
        "counts": counts,
    }
    latest_path = out / "latest.json"
    latest_path.write_text(
        json.dumps(latest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out / f"{zip_name}.sha256").write_text(
        f"{package_sha}  {zip_name}\n", encoding="utf-8"
    )
    (out / "release-notes.md").write_text(
        "# WRCA data package\n\n"
        f"- Package: {args.package_version}\n"
        f"- Wild Rift patch: {patch}\n"
        f"- Champions: {counts['champions']}\n"
        f"- Matchups: {counts['matchups']}\n"
        f"- WRC role builds: {counts['role_builds']}\n"
        f"- WRC build cache: {counts['build_cache_champions']}/{counts['champions']}\n"
        f"- WRC matchup cache: {counts['matchup_cache_champions']}/{counts['champions']}\n"
        f"- Champion icons: {champ_icons}\n"
        f"- Item icons: {item_icons}\n"
        f"- Rune role pages: {counts['rune_role_pairs']}\n"
        f"- Rune variants: {counts['role_rune_variants']}\n"
        f"- Situational rune rules: {counts['role_rune_adaptations']}\n"
        f"- Localized runes: {counts['localized_runes']}\n"
        f"- Rune icon files: {rune_icons}\n"
        f"- SHA-256: {package_sha}\n",
        encoding="utf-8",
    )
    print(json.dumps(latest, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
