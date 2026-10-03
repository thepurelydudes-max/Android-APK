from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

RUNE_TABLES = (
    "runes",
    "role_runes",
    "role_rune_variants",
    "role_rune_adaptations",
)
SOURCE_PRIORITY = {
    "wrpocket.app:tencent-cn": 0,
    "wildriftfire.com": 1,
    "wildriftcore.com:indexed-7.3a": 2,
}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _copy_tables(target_db: Path, source_db: Path) -> None:
    with sqlite3.connect(target_db) as con:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("ATTACH DATABASE ? AS rune_seed", (str(source_db),))
        try:
            source_tables = {
                str(row[0])
                for row in con.execute(
                    "SELECT name FROM rune_seed.sqlite_master WHERE type='table'"
                )
            }
            missing = [name for name in RUNE_TABLES if name not in source_tables]
            if missing:
                raise RuntimeError("Rune seed is missing tables: " + ", ".join(missing))

            for table in reversed(RUNE_TABLES):
                con.execute(f"DROP TABLE IF EXISTS main.{_q(table)}")

            for table in RUNE_TABLES:
                row = con.execute(
                    "SELECT sql FROM rune_seed.sqlite_master "
                    "WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not row or not row[0]:
                    raise RuntimeError(f"Rune seed schema missing for {table}")
                con.execute(str(row[0]))
                con.execute(
                    f"INSERT INTO {_q(table)} SELECT * FROM rune_seed.{_q(table)}"
                )

            # Recreate indexes owned by copied rune tables.
            for row in con.execute(
                "SELECT name,sql,tbl_name FROM rune_seed.sqlite_master "
                "WHERE type='index' AND sql IS NOT NULL"
            ).fetchall():
                name, sql, table = str(row[0]), str(row[1]), str(row[2])
                if table in RUNE_TABLES:
                    con.execute(f"DROP INDEX IF EXISTS main.{_q(name)}")
                    con.execute(sql)

            # Rune localization metadata belongs to the rune dataset.
            for key in ("rune_locale_ru", "rune_locale_ru_patch"):
                row = con.execute(
                    "SELECT value FROM rune_seed.meta WHERE key=?", (key,)
                ).fetchone()
                if row:
                    con.execute(
                        "INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(row[0])),
                    )
            con.commit()
        finally:
            con.execute("DETACH DATABASE rune_seed")
            con.execute("PRAGMA foreign_keys=ON")


def _audit(database: Path, cache: Path) -> dict:
    with sqlite3.connect(database) as con:
        con.row_factory = sqlite3.Row
        quick = str(con.execute("PRAGMA quick_check").fetchone()[0])
        if quick.casefold() != "ok":
            raise RuntimeError(f"SQLite quick_check failed: {quick}")

        build_pairs = {
            (str(r[0]), str(r[1]))
            for r in con.execute(
                "SELECT DISTINCT champion_id,role FROM role_builds "
                "WHERE source='wildriftcore.com'"
            )
        }
        selected: dict[tuple[str, str], tuple[list[str], str]] = {}
        for row in con.execute(
            "SELECT champion_id,role,runes_json,source FROM role_runes"
        ):
            source = str(row["source"])
            if source not in SOURCE_PRIORITY:
                continue
            key = (str(row["champion_id"]), str(row["role"]))
            runes = [str(v) for v in json.loads(row["runes_json"] or "[]")]
            old = selected.get(key)
            if old is None or SOURCE_PRIORITY[source] < SOURCE_PRIORITY[old[1]]:
                selected[key] = (runes, source)

        missing_pairs = sorted(build_pairs - set(selected))
        if missing_pairs:
            raise RuntimeError(f"Rune coverage missing role pairs: {missing_pairs}")

        bad_pages = [
            (key, values[0], values[1])
            for key, values in sorted(selected.items())
            if key in build_pairs and (
                len(values[0]) != 5 or len(set(values[0])) != 5
            )
        ]
        if bad_pages:
            raise RuntimeError(f"Incomplete rune pages: {bad_pages}")

        used = {
            name
            for key, (runes, _source) in selected.items()
            if key in build_pairs
            for name in runes
        }
        missing_locale = []
        missing_catalog = []
        for name in sorted(used):
            row = con.execute(
                "SELECT name_ru,effect_ru,effect_en,icon_path FROM runes WHERE name=?",
                (name,),
            ).fetchone()
            if row is None:
                missing_catalog.append(name)
                continue
            if (
                not str(row["name_ru"] or "").strip()
                or not str(row["effect_ru"] or "").strip()
                or not str(row["effect_en"] or "").strip()
            ):
                missing_locale.append(name)
        if missing_catalog:
            raise RuntimeError("Missing runes from catalog: " + ", ".join(missing_catalog))
        if missing_locale:
            raise RuntimeError("Missing RU rune localization: " + ", ".join(missing_locale))

        counts = {
            "role_build_pairs": len(build_pairs),
            "covered_pairs": len(build_pairs & set(selected)),
            "five_rune_pages": sum(
                1 for key, (runes, _source) in selected.items()
                if key in build_pairs and len(runes) == 5 and len(set(runes)) == 5
            ),
            "unique_runes": int(con.execute("SELECT COUNT(*) FROM runes").fetchone()[0]),
            "used_runes": len(used),
            "role_rune_rows": int(con.execute("SELECT COUNT(*) FROM role_runes").fetchone()[0]),
            "role_rune_variants": int(con.execute("SELECT COUNT(*) FROM role_rune_variants").fetchone()[0]),
            "situational_rune_rules": int(con.execute("SELECT COUNT(*) FROM role_rune_adaptations").fetchone()[0]),
        }

    icon_files = list((cache / "runes").glob("*"))
    counts["rune_icon_files"] = sum(1 for p in icon_files if p.is_file())
    if counts["role_build_pairs"] != 226 or counts["covered_pairs"] != 226 or counts["five_rune_pages"] != 226:
        raise RuntimeError(f"Rune coverage audit failed: {counts}")
    if counts["rune_icon_files"] <= 0:
        raise RuntimeError("Rune icon cache is empty")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets-root", required=True)
    ap.add_argument("--seed-root", required=True)
    args = ap.parse_args()

    assets = Path(args.assets_root).resolve()
    seed = Path(args.seed_root).resolve()
    target_db = assets / "data" / "wildrift.db"
    source_db = seed / "data" / "wildrift.db"
    target_cache = assets / "cache"
    source_runes = seed / "cache" / "runes"

    if not target_db.is_file():
        raise SystemExit(f"Target DB missing: {target_db}")
    if not source_db.is_file():
        raise SystemExit(f"Rune seed DB missing: {source_db}")
    if not source_runes.is_dir():
        raise SystemExit(f"Rune icon seed missing: {source_runes}")

    _copy_tables(target_db, source_db)
    dst = target_cache / "runes"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(source_runes, dst)

    audit = _audit(target_db, target_cache)
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
