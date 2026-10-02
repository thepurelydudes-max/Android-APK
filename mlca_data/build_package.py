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
DEFAULT_MIN_APP_VERSION = "2.1.7"
REPO = "thepurelydudes-max/Android-APK"


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

        tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = (
            "champions", "stats", "champion_tiers", "matchups",
            "matchup_evidence", "matchup_directions", "items",
            "counter_items", "role_builds", "role_build_variants",
            "role_build_situational", "role_build_boots", "meta",
        )
        missing = [name for name in required if name not in tables]
        if missing:
            raise SystemExit("Missing DB tables: " + ", ".join(missing))

        counts = {
            table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in required if table != "meta"
        }
        counts["role_build_champions"] = int(
            con.execute("SELECT COUNT(DISTINCT champion_id) FROM role_builds").fetchone()[0]
        )
        counts["tier_roles"] = int(
            con.execute("SELECT COUNT(DISTINCT role) FROM champion_tiers").fetchone()[0]
        )
        meta = {str(k): str(v) for k, v in con.execute("SELECT key,value FROM meta")}
        return counts, meta


def validate_complete(
    database: Path,
    counts: dict[str, int],
    meta: dict[str, str],
    champion_icons: int,
    item_icons: int,
) -> None:
    checks = {
        "champions": counts["champions"] >= 130,
        "stats": counts["stats"] > 0,
        "champion_tiers": counts["champion_tiers"] > 0,
        "tier_roles": counts["tier_roles"] >= 5,
        "matchups": counts["matchups"] >= 1850,
        "matchup_evidence": counts["matchup_evidence"] >= 1850,
        "matchup_directions": counts["matchup_directions"] >= 900,
        "items": counts["items"] >= 45,
        "counter_items": counts["counter_items"] > 0,
        "role_builds": counts["role_builds"] >= 160,
        "role_build_variants": counts["role_build_variants"] >= counts["role_builds"],
        "role_build_situational": counts["role_build_situational"] > 0,
        "role_build_boots": counts["role_build_boots"] > 0,
        "champion_icons": champion_icons >= counts["champions"],
        "item_icons": item_icons >= 45,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        details = ", ".join(f"{key}={counts.get(key, '-')}" for key in failed)
        raise SystemExit("MLCA package completeness checks failed: " + details)

    if meta.get("matchup_contract_version") != "2":
        raise SystemExit("MLCA package requires matchup contract v2")
    if meta.get("matchup_evidence_source") != "mlbbhub.matchups":
        raise SystemExit("MLCA package matchup source is not mlbbhub.matchups")
    if meta.get("matchup_directional_source") != "mlbbhub.matchups":
        raise SystemExit("MLCA package counter-list source is not mlbbhub.matchups")
    if meta.get("matchup_direction") != "row_hero_to_column_hero":
        raise SystemExit("MLCA package matchup direction is invalid")
    if meta.get("matchup_sample_window") != "ranked-current":
        raise SystemExit("MLCA package matchup sample window is invalid")
    patch = str(meta.get("patch_version") or "").strip()
    if not patch:
        raise SystemExit("MLCA patch_version is missing")

    with sqlite3.connect(database) as con:
        for champion_name, enemy_name in (
            ("Paquito", "Karina"),
            ("Minotaur", "Lolita"),
            ("Obsidia", "Benedetta"),
            ("Obsidia", "Aldous"),
        ):
            row = con.execute(
                "SELECT e.raw_edge FROM matchup_evidence e "
                "JOIN champions c ON c.id=e.champion_id "
                "JOIN champions x ON x.id=e.enemy_id "
                "WHERE lower(c.name)=lower(?) AND lower(x.name)=lower(?) "
                "ORDER BY e.confidence DESC LIMIT 1",
                (champion_name, enemy_name),
            ).fetchone()
            if row is None or float(row[0]) <= 0:
                raise SystemExit(
                    f"MLCA direction sentinel failed: {champion_name}->{enemy_name}={row}"
                )


def write_package_metadata(
    database: Path,
    *,
    package_version: str,
    generated_at: datetime,
) -> None:
    iso_time = generated_at.astimezone(timezone.utc).isoformat()
    display_time = generated_at.astimezone(timezone.utc).strftime("%d-%m-%Y %H:%M")
    rows = {
        "package_version": package_version,
        "package_generated_at": iso_time,
        "package_protocol_version": str(PROTOCOL_VERSION),
        "last_update": display_time,
        "last_update_iso": iso_time,
    }
    with sqlite3.connect(database) as con:
        con.executemany(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            rows.items(),
        )
        con.commit()
        if str(con.execute("PRAGMA quick_check").fetchone()[0]).casefold() != "ok":
            raise SystemExit("SQLite quick_check failed after package metadata update")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-root", default="mlbb/assets")
    parser.add_argument("--output", default="dist/mlca-data")
    parser.add_argument("--package-version", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--min-app-version", default=DEFAULT_MIN_APP_VERSION)
    args = parser.parse_args()

    assets = Path(args.assets_root).resolve()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    database = assets / "data" / "mobilelegends.db"
    cache = assets / "cache"
    if not database.is_file() or not cache.is_dir():
        raise SystemExit("Expected mobilelegends.db and cache/ in MLCA assets root")

    generated_at = datetime.now(timezone.utc)
    write_package_metadata(
        database,
        package_version=args.package_version,
        generated_at=generated_at,
    )
    counts, meta = db_audit(database)

    champion_icons = len(list((cache / "champions").glob("*.png")))
    item_icons = len(list((cache / "items").glob("*.png")))
    validate_complete(database, counts, meta, champion_icons, item_icons)
    counts["champion_icons"] = champion_icons
    counts["item_icons"] = item_icons

    patch = str(meta.get("patch_version") or "").strip()
    internal = {
        "protocol_version": PROTOCOL_VERSION,
        "package_version": args.package_version,
        "min_app_version": args.min_app_version,
        "patch": patch,
        "generated_at_utc": generated_at.isoformat(),
        "source_revision": os.environ.get("GITHUB_SHA", ""),
        "database_sha256": sha256(database),
        "cache_sha256": tree_sha256(cache),
        "counts": counts,
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(
        json.dumps(internal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    zip_name = f"MLCA-data-{args.package_version}.zip"
    zip_path = out / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(manifest_path, "manifest.json")
        archive.write(database, "data/mobilelegends.db")
        for path in sorted(cache.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(assets).as_posix())

    package_sha = sha256(zip_path)
    manifest_sha = sha256(manifest_path)
    latest = {
        "protocol_version": PROTOCOL_VERSION,
        "package_version": args.package_version,
        "min_app_version": args.min_app_version,
        "patch": patch,
        "generated_at_utc": internal["generated_at_utc"],
        "release_tag": args.release_tag,
        "download_url": (
            f"https://github.com/{REPO}/releases/download/"
            f"{args.release_tag}/{zip_name}"
        ),
        "size_bytes": zip_path.stat().st_size,
        "sha256": package_sha,
        "manifest_sha256": manifest_sha,
        "counts": counts,
    }
    (out / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out / f"{zip_name}.sha256").write_text(
        f"{package_sha}  {zip_name}\n",
        encoding="utf-8",
    )
    (out / "release-notes.md").write_text(
        "# MLCA data package\n\n"
        f"- Package: {args.package_version}\n"
        f"- MLBB patch: {patch}\n"
        f"- Heroes: {counts['champions']}\n"
        f"- Measured matchups: {counts['matchup_evidence']}\n"
        f"- Direction-only counters: {counts['matchup_directions']}\n"
        f"- Role builds: {counts['role_builds']}\n"
        f"- Champion icons: {champion_icons}\n"
        f"- Item icons: {item_icons}\n"
        f"- SHA-256: {package_sha}\n",
        encoding="utf-8",
    )
    print(json.dumps(latest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
