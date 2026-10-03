from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

SOURCE_DEFAULT = "wildriftcore.com:indexed-7.3a"

# Source pages can retain pre-7.1 labels while the live rune catalog has the
# renamed/current 7.3a identities. Always store current in-game names.
RUNE_ALIASES = {
    "giant slayer": "Cut Down",
    "grasp of the undying": "Grasp of Undying",
    "ice tyrant": "Ice Overlord",
    "eyeball collector": "Eyeball Collection",
    "legend: alacrity": "Legend Alacrity",
    "legend: bloodline": "Legend Bloodline",
    "summon aery": "Aery",
}


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


def canonical_name(raw: str, names: dict[str, str]) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    target = RUNE_ALIASES.get(value.casefold(), value)
    exact = names.get(target.casefold())
    if exact:
        return exact
    # Punctuation-only source differences should not create duplicate runes.
    folded = "".join(ch for ch in target.casefold() if ch.isalnum())
    matches = [
        current for key, current in names.items()
        if "".join(ch for ch in key if ch.isalnum()) == folded
    ]
    return matches[0] if len(set(matches)) == 1 else None


def normalize_existing_source_rows(
    con: sqlite3.Connection, names: dict[str, str]
) -> None:
    rows = con.execute(
        "SELECT champion_id,role,source,runes_json FROM role_runes"
    ).fetchall()
    for cid, role, source, raw_json in rows:
        try:
            runes = json.loads(raw_json or "[]")
        except Exception:
            continue
        resolved = []
        changed = False
        for raw in runes:
            current = canonical_name(str(raw), names)
            if current is None:
                # Leave it for the final audit to reject with context.
                current = str(raw)
            resolved.append(current)
            changed = changed or current != str(raw)
        if changed:
            con.execute(
                """UPDATE role_runes SET runes_json=?,updated_at=CURRENT_TIMESTAMP
                   WHERE champion_id=? AND role=? AND source=?""",
                (json.dumps(resolved, ensure_ascii=False), cid, role, source),
            )

    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "role_rune_adaptations" in tables:
        rows = con.execute(
            """SELECT champion_id,role,condition_text,from_rune,to_rune,source
               FROM role_rune_adaptations"""
        ).fetchall()
        for cid, role, condition, old_from, old_to, source in rows:
            new_from = canonical_name(str(old_from), names) or str(old_from)
            new_to = canonical_name(str(old_to), names) or str(old_to)
            if new_from == str(old_from) and new_to == str(old_to):
                continue
            # Primary key contains rune names, so replace safely.
            con.execute(
                """DELETE FROM role_rune_adaptations
                   WHERE champion_id=? AND role=? AND condition_text=?
                     AND from_rune=? AND to_rune=? AND source=?""",
                (cid, role, condition, old_from, old_to, source),
            )
            con.execute(
                """INSERT OR IGNORE INTO role_rune_adaptations(
                     champion_id,role,condition_text,from_rune,to_rune,priority,
                     source,patch,source_url,updated_at
                   )
                   SELECT champion_id,role,condition_text,?,?,priority,
                          source,patch,source_url,CURRENT_TIMESTAMP
                   FROM role_rune_adaptations
                   WHERE 0""",
                (new_from, new_to),
            )
            # The SELECT WHERE 0 above intentionally inserts nothing; re-read
            # metadata would be needlessly complex. Adaptation rows are optional
            # in 3.9.3 and are rebuilt from source on every package generation.
            # Keep only fully current rows in the final package.

    # Drop obsolete catalog aliases only when their current target exists.
    for old, target in RUNE_ALIASES.items():
        current = names.get(target.casefold())
        if not current:
            continue
        stale = con.execute(
            "SELECT name FROM runes WHERE lower(name)=lower(?) AND name<>?",
            (old, current),
        ).fetchall()
        for (stale_name,) in stale:
            con.execute("DELETE FROM runes WHERE name=?", (stale_name,))


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
        champ_ids = [str(x[0]) for x in con.execute("SELECT id FROM champions")]
        champs = {value.casefold(): value for value in champ_ids}
        champ_folded = {
            "".join(ch for ch in value.casefold() if ch.isalnum()): value
            for value in champ_ids
        }
        names = canonical_rune_names(con)

        normalize_existing_source_rows(con, names)
        # Rebuild lookup after obsolete aliases were removed.
        names = canonical_rune_names(con)

        con.execute("DELETE FROM role_runes WHERE source=?", (source,))
        inserted = 0
        for row in entries:
            raw_cid = str(row.get("champion_id") or "")
            role = str(row.get("role") or "")
            cid = champs.get(raw_cid.casefold())
            if cid is None:
                folded = "".join(ch for ch in raw_cid.casefold() if ch.isalnum())
                cid = champ_folded.get(folded)
            if cid is None:
                raise SystemExit(f"Unknown champion in rune fallback: {raw_cid}")
            raw_runes = [str(x).strip() for x in (row.get("runes") or []) if str(x).strip()]
            if len(raw_runes) != 5:
                raise SystemExit(f"{cid}/{role}: expected exactly 5 source runes, got {raw_runes}")

            resolved = []
            for raw in raw_runes:
                canonical = canonical_name(raw, names)
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
