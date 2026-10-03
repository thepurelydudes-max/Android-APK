from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

SOURCE_DEFAULT = "wildriftcore.com:indexed-7.3a"


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS runes (
            name TEXT PRIMARY KEY,
            name_ru TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            effect_en TEXT NOT NULL DEFAULT '',
            icon_url TEXT NOT NULL DEFAULT '',
            icon_path TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            patch TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS role_runes (
            champion_id TEXT NOT NULL,
            role TEXT NOT NULL,
            runes_json TEXT NOT NULL DEFAULT '[]',
            pick_rate REAL,
            win_rate REAL,
            rank_band TEXT NOT NULL DEFAULT '',
            sample_date TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            patch TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (champion_id, role, source),
            FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
        );
        """
    )


def canonical_rune_names(con: sqlite3.Connection) -> dict[str, str]:
    rows = con.execute("SELECT name FROM runes ORDER BY name").fetchall()
    out: dict[str, str] = {}
    for (name,) in rows:
        value = str(name)
        out.setdefault(value.casefold(), value)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--fallback", default=str(Path(__file__).with_name("wrc_rune_fallback_7_3a.json")))
    args = ap.parse_args()

    payload = json.loads(Path(args.fallback).read_text(encoding="utf-8"))
    patch = str(payload.get("patch") or "7.3a")
    source = str(payload.get("source") or SOURCE_DEFAULT)
    entries = list(payload.get("entries") or [])

    with sqlite3.connect(args.db) as con:
        con.execute("PRAGMA foreign_keys=ON")
        ensure_schema(con)
        champs = {str(x[0]) for x in con.execute("SELECT id FROM champions")}
        names = canonical_rune_names(con)

        # Primary source uses one historical capitalization difference.
        aliases = {
            "coup de grace": "coup de grace",
            "coup de grace ": "coup de grace",
            "courage of the colossus": "courage of the colossus",
        }

        con.execute("DELETE FROM role_runes WHERE source=?", (source,))
        inserted = 0
        for row in entries:
            cid = str(row.get("champion_id") or "")
            role = str(row.get("role") or "")
            if cid not in champs:
                raise SystemExit(f"Unknown champion in rune fallback: {cid}")
            raw_runes = [str(x).strip() for x in (row.get("runes") or []) if str(x).strip()]
            if len(raw_runes) not in (4, 5):
                raise SystemExit(f"{cid}/{role}: expected 4 or 5 source runes, got {raw_runes}")

            resolved = []
            for raw in raw_runes:
                key = aliases.get(raw.casefold(), raw.casefold())
                canonical = names.get(key)
                if canonical is None:
                    # Do not invent a catalog row: media/catalog must already
                    # have verified this rune through a collected source.
                    raise SystemExit(
                        f"{cid}/{role}: rune not present in collected catalog: {raw!r}"
                    )
                resolved.append(canonical)

            # Fallback must never overwrite a stronger exact role source.
            existing = con.execute(
                """SELECT source FROM role_runes
                   WHERE champion_id=? AND role=?
                     AND source IN ('wrpocket.app:tencent-cn','wildriftfire.com')
                   LIMIT 1""",
                (cid, role),
            ).fetchone()
            if existing:
                continue

            con.execute(
                """INSERT INTO role_runes(
                     champion_id,role,runes_json,pick_rate,win_rate,rank_band,sample_date,
                     source,patch,source_url,updated_at
                   ) VALUES(?,?,?,NULL,NULL,'Role guide','',?,?,?,CURRENT_TIMESTAMP)""",
                (
                    cid, role, json.dumps(resolved, ensure_ascii=False),
                    source, patch, str(row.get("source_url") or ""),
                ),
            )
            inserted += 1

        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        if str(quick).casefold() != "ok":
            raise SystemExit(f"SQLite quick_check failed: {quick}")

    print(json.dumps({
        "fallback_entries": len(entries),
        "inserted": inserted,
        "source": source,
        "patch": patch,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
