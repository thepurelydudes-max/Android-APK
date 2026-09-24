from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from localization import COMMON_CHAMPION_ALIASES, normalize_search
from paths import APP_DIR

DB_PATH = APP_DIR / "wildrift.db"


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context manager also releases the file handle."""

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, factory=ClosingConnection)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _ensure_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS champions (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, roles_json TEXT NOT NULL DEFAULT '[]',
                lanes_json TEXT NOT NULL DEFAULT '[]', damage_type TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS champion_aliases (
                champion_id TEXT NOT NULL, alias TEXT NOT NULL, alias_norm TEXT NOT NULL,
                PRIMARY KEY (champion_id, alias_norm),
                FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_champion_alias_norm ON champion_aliases(alias_norm);
            CREATE TABLE IF NOT EXISTS stats (
                champion_id TEXT NOT NULL, lane TEXT NOT NULL, rank_segment TEXT NOT NULL,
                win_rate REAL, pick_rate REAL, ban_rate REAL, date TEXT,
                PRIMARY KEY (champion_id, lane, rank_segment),
                FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS matchups (
                champion_id TEXT NOT NULL, enemy_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY (champion_id, enemy_id, role, source)
            );
            CREATE TABLE IF NOT EXISTS champion_tiers (
                champion_id TEXT NOT NULL, role TEXT NOT NULL, tier TEXT NOT NULL,
                source TEXT NOT NULL, patch TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (champion_id, role, source),
                FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_champion_tiers_role_tier
                ON champion_tiers(role, tier);
            CREATE TABLE IF NOT EXISTS matchup_page_cache (
                source TEXT NOT NULL, patch TEXT NOT NULL, champion_id TEXT NOT NULL,
                rows_json TEXT NOT NULL DEFAULT '[]', source_url TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source, patch, champion_id)
            );
            CREATE INDEX IF NOT EXISTS idx_matchup_page_cache_source_patch
                ON matchup_page_cache(source, patch);
            CREATE TABLE IF NOT EXISTS item_pools (
                champion_id TEXT NOT NULL, item_name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 999, source TEXT NOT NULL,
                PRIMARY KEY (champion_id, item_name, source)
            );
            CREATE TABLE IF NOT EXISTS counter_items (
                enemy_id TEXT NOT NULL, item_name TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL, PRIMARY KEY (enemy_id, item_name, source)
            );
            CREATE TABLE IF NOT EXISTS items (
                name TEXT PRIMARY KEY, category TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS media_assets (
                asset_key TEXT PRIMARY KEY, source_url TEXT NOT NULL DEFAULT '',
                etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT '',
                content_length INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '',
                local_path TEXT NOT NULL DEFAULT '', checked_patch TEXT NOT NULL DEFAULT '',
                last_checked TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        _ensure_column(con, "champions", "name_ru", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "champions", "icon_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "champions", "icon_path", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "name_ru", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "icon_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "icon_path", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "price", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(con, "items", "stats_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(con, "items", "effect_en", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "data_hash", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "data_patch", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "data_source_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(con, "items", "tier", "TEXT NOT NULL DEFAULT ''")

        # Backfill aliases for databases created by the original MVP.
        for row in con.execute("SELECT id,name,name_ru FROM champions").fetchall():
            values = [row["id"], row["name"], row["name_ru"], *COMMON_CHAMPION_ALIASES.get(row["id"], ())]
            seen = set()
            for alias in values:
                norm = normalize_search(alias or "")
                if norm and norm not in seen:
                    seen.add(norm)
                    con.execute(
                        "INSERT OR IGNORE INTO champion_aliases(champion_id,alias,alias_norm) VALUES(?,?,?)",
                        (row["id"], alias, norm),
                    )


def set_meta(key: str, value: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(key: str, default: str = "") -> str:
    with connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def upsert_champion(champ_id: str, name: str, roles: list[str], lanes: list[str], damage_type: str, source: str, updated_at: str = "", name_ru: str = "", icon_url: str = "", icon_path: str = "") -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO champions(id,name,roles_json,lanes_json,damage_type,source,updated_at,name_ru,icon_url,icon_path)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name, roles_json=excluded.roles_json, lanes_json=excluded.lanes_json,
            damage_type=excluded.damage_type, source=excluded.source, updated_at=excluded.updated_at,
            name_ru=CASE WHEN excluded.name_ru<>'' THEN excluded.name_ru ELSE champions.name_ru END,
            icon_url=CASE WHEN excluded.icon_url<>'' THEN excluded.icon_url ELSE champions.icon_url END,
            icon_path=CASE WHEN excluded.icon_path<>'' THEN excluded.icon_path ELSE champions.icon_path END""",
            (champ_id, name, json.dumps(roles, ensure_ascii=False), json.dumps(lanes, ensure_ascii=False), damage_type or "", source, updated_at or "", name_ru or "", icon_url or "", icon_path or ""),
        )
    aliases = [champ_id, name, name_ru, *COMMON_CHAMPION_ALIASES.get(champ_id, ())]
    replace_champion_aliases(champ_id, [x for x in aliases if x])


def replace_champion_aliases(champion_id: str, aliases: Iterable[str]) -> None:
    rows = []
    seen = set()
    for alias in aliases:
        norm = normalize_search(alias)
        if norm and norm not in seen:
            seen.add(norm); rows.append((champion_id, alias, norm))
    with connect() as con:
        for row in rows:
            con.execute("INSERT OR IGNORE INTO champion_aliases(champion_id,alias,alias_norm) VALUES(?,?,?)", row)


def upsert_item(
    name: str, category: str = "", source: str = "", name_ru: str = "",
    icon_url: str = "", icon_path: str = "", tier: str = "",
) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO items(name,category,source,name_ru,icon_url,icon_path,tier) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET category=excluded.category, source=excluded.source,
            name_ru=CASE WHEN excluded.name_ru<>'' THEN excluded.name_ru ELSE items.name_ru END,
            icon_url=CASE WHEN excluded.icon_url<>'' THEN excluded.icon_url ELSE items.icon_url END,
            icon_path=CASE WHEN excluded.icon_path<>'' THEN excluded.icon_path ELSE items.icon_path END,
            tier=CASE WHEN excluded.tier<>'' THEN excluded.tier ELSE items.tier END""",
            (name, category or "", source or "", name_ru or "", icon_url or "", icon_path or "", tier or ""),
        )


def upsert_item_details(
    name: str, *, price: int = 0, stats: Iterable[str] = (), effect_en: str = "",
    data_hash: str = "", data_patch: str = "", source_url: str = "",
) -> bool:
    """Update item characteristics only when their normalized data hash changed.

    Returns True when item detail columns were written. Existing good data is left
    untouched when the caller presents the same hash again.
    """
    clean_stats = [str(x).strip() for x in stats if str(x).strip()]
    with connect() as con:
        row = con.execute("SELECT data_hash,data_patch,data_source_url FROM items WHERE name=?", (name,)).fetchone()
        if row and data_hash and (row["data_hash"] or "") == data_hash:
            # Content is unchanged, but remember that this exact content was
            # verified for the current patch/source URL so the fallback does
            # not re-fetch it on every subsequent update.
            if (data_patch and (row["data_patch"] or "") != data_patch) or (source_url and (row["data_source_url"] or "") != source_url):
                con.execute(
                    "UPDATE items SET data_patch=CASE WHEN ?<>'' THEN ? ELSE data_patch END, "
                    "data_source_url=CASE WHEN ?<>'' THEN ? ELSE data_source_url END WHERE name=?",
                    (data_patch or "", data_patch or "", source_url or "", source_url or "", name),
                )
            return False
        if row is None:
            con.execute("INSERT INTO items(name,source) VALUES(?,?)", (name, "wrpocket.app"))
        con.execute(
            """UPDATE items SET price=?,stats_json=?,effect_en=?,data_hash=?,data_patch=?,data_source_url=?
               WHERE name=?""",
            (int(price or 0), json.dumps(clean_stats, ensure_ascii=False), effect_en or "",
             data_hash or "", data_patch or "", source_url or "", name),
        )
    return True


def item_catalog_rows() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM items ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def upsert_stat(champion_id: str, lane: str, rank_segment: str, win_rate: Optional[float], pick_rate: Optional[float], ban_rate: Optional[float], date: str = "") -> None:
    with connect() as con:
        con.execute("""INSERT INTO stats(champion_id,lane,rank_segment,win_rate,pick_rate,ban_rate,date) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(champion_id,lane,rank_segment) DO UPDATE SET win_rate=excluded.win_rate,pick_rate=excluded.pick_rate,ban_rate=excluded.ban_rate,date=excluded.date""",
        (champion_id, lane, rank_segment, win_rate, pick_rate, ban_rate, date))


def replace_source_matchups(source: str, rows: Iterable[tuple[str, str, str, float]]) -> None:
    with connect() as con:
        con.execute("DELETE FROM matchups WHERE source=?", (source,))
        con.executemany("INSERT OR REPLACE INTO matchups(champion_id,enemy_id,role,score,source) VALUES(?,?,?,?,?)", [(a,b,r,s,source) for a,b,r,s in rows])


def get_matchup_page_cache(source: str, patch: str) -> dict[str, list[tuple[str, str, str, float]]]:
    """Return successfully parsed per-champion matchup pages for one patch."""
    with connect() as con:
        rows = con.execute(
            "SELECT champion_id,rows_json FROM matchup_page_cache WHERE source=? AND patch=?",
            (source, patch or ""),
        ).fetchall()
    out: dict[str, list[tuple[str, str, str, float]]] = {}
    for row in rows:
        try:
            raw = json.loads(row["rows_json"] or "[]")
            parsed = []
            for item in raw:
                if not isinstance(item, (list, tuple)) or len(item) != 4:
                    continue
                parsed.append((str(item[0]), str(item[1]), str(item[2]), float(item[3])))
            if parsed:
                out[str(row["champion_id"])] = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return out


def upsert_matchup_page_cache(
    source: str, patch: str, champion_id: str, rows: Iterable[tuple[str, str, str, float]], source_url: str = ""
) -> None:
    """Persist one successfully parsed page immediately so interrupted updates can resume."""
    payload = json.dumps(list(rows), ensure_ascii=False, separators=(",", ":"))
    with connect() as con:
        con.execute(
            """INSERT INTO matchup_page_cache(source,patch,champion_id,rows_json,source_url,fetched_at)
               VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(source,patch,champion_id) DO UPDATE SET
               rows_json=excluded.rows_json, source_url=excluded.source_url, fetched_at=CURRENT_TIMESTAMP""",
            (source, patch or "", champion_id, payload, source_url or ""),
        )


def prune_matchup_page_cache(source: str, keep_patch: str) -> None:
    """Keep only the current patch cache after a complete successful refresh."""
    with connect() as con:
        con.execute("DELETE FROM matchup_page_cache WHERE source=? AND patch<>?", (source, keep_patch or ""))


def replace_source_champion_tiers(source: str, rows: Iterable[tuple[str, str, str]], patch: str = "") -> None:
    """Replace one source's role-specific champion tier list atomically."""
    clean_rows = []
    for champion_id, role, tier in rows:
        tier_value = str(tier or "").strip().upper()
        if tier_value not in {"S+", "S", "A", "B", "C", "D"}:
            continue
        clean_rows.append((champion_id, role, tier_value, source, patch or ""))
    with connect() as con:
        con.execute("DELETE FROM champion_tiers WHERE source=?", (source,))
        con.executemany(
            """INSERT OR REPLACE INTO champion_tiers(champion_id,role,tier,source,patch,updated_at)
               VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)""",
            clean_rows,
        )


def replace_source_champion_tiers_partial(source: str, rows: Iterable[tuple[str, str, str]], patch: str = "") -> None:
    """Replace only roles present in ``rows`` and keep other source roles intact.

    Tier pages are fetched independently.  If one role is temporarily blocked
    or its markup changes, a successful refresh of the other four roles should
    not erase the last known data for the failed role.
    """
    clean_rows = []
    roles: set[str] = set()
    for champion_id, role, tier in rows:
        tier_value = str(tier or "").strip().upper()
        role_value = str(role or "").strip()
        if not role_value or tier_value not in {"S+", "S", "A", "B", "C", "D"}:
            continue
        roles.add(role_value)
        clean_rows.append((champion_id, role_value, tier_value, source, patch or ""))
    if not clean_rows:
        return
    with connect() as con:
        for role in sorted(roles):
            con.execute("DELETE FROM champion_tiers WHERE source=? AND role=?", (source, role))
        con.executemany(
            """INSERT OR REPLACE INTO champion_tiers(champion_id,role,tier,source,patch,updated_at)
               VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)""",
            clean_rows,
        )


def get_champion_tier(champion_id: str, role: str, source: str = "wildriftcore.com") -> str:
    with connect() as con:
        row = con.execute(
            "SELECT tier FROM champion_tiers WHERE champion_id=? AND role=? AND source=?",
            (champion_id, role, source),
        ).fetchone()
        if row:
            return str(row[0] or "")
        # Future-proof fallback if another tier source is ever added.
        row = con.execute(
            "SELECT tier FROM champion_tiers WHERE champion_id=? AND role=? ORDER BY updated_at DESC LIMIT 1",
            (champion_id, role),
        ).fetchone()
    return str(row[0] or "") if row else ""


def replace_source_item_pools(source: str, rows: Iterable[tuple[str, str, str, int]]) -> None:
    with connect() as con:
        con.execute("DELETE FROM item_pools WHERE source=?", (source,))
        con.executemany("INSERT OR REPLACE INTO item_pools(champion_id,item_name,category,priority,source) VALUES(?,?,?,?,?)", [(c,i,cat,pr,source) for c,i,cat,pr in rows])


def replace_source_counter_items(source: str, rows: Iterable[tuple[str, str, str]]) -> None:
    with connect() as con:
        con.execute("DELETE FROM counter_items WHERE source=?", (source,))
        con.executemany("INSERT OR REPLACE INTO counter_items(enemy_id,item_name,reason,source) VALUES(?,?,?,?)", [(e,i,r,source) for e,i,r in rows])


def replace_source_items(source: str, rows: Iterable[tuple]) -> None:
    """Refresh a source catalog without discarding cached media for unchanged items."""
    parsed = []
    for row in rows:
        name = row[0]
        category = row[1] if len(row) > 1 else ""
        name_ru = row[2] if len(row) > 2 else ""
        icon_url = row[3] if len(row) > 3 else ""
        tier = row[4] if len(row) > 4 else ""
        parsed.append((name, category, name_ru, icon_url, tier))
    incoming = {row[0] for row in parsed}
    for name, category, name_ru, icon_url, tier in parsed:
        upsert_item(name, category, source, name_ru=name_ru, icon_url=icon_url, tier=tier)
    with connect() as con:
        if incoming:
            placeholders = ",".join("?" for _ in incoming)
            con.execute(
                f"DELETE FROM items WHERE source=? AND name NOT IN ({placeholders})",
                (source, *sorted(incoming)),
            )
        else:
            con.execute("DELETE FROM items WHERE source=?", (source,))


def champions() -> list[dict]:
    with connect() as con:
        rows = con.execute("SELECT * FROM champions ORDER BY name COLLATE NOCASE").fetchall()
    out=[]
    for r in rows:
        d=dict(r); d["roles"]=json.loads(d.pop("roles_json") or "[]"); d["lanes"]=json.loads(d.pop("lanes_json") or "[]"); out.append(d)
    return out


def champion_by_name_or_id(value: str) -> Optional[dict]:
    needle = normalize_search(value)
    if not needle:
        return None
    all_champs = {c["id"]: c for c in champions()}
    with connect() as con:
        exact = con.execute("SELECT champion_id FROM champion_aliases WHERE alias_norm=?", (needle,)).fetchall()
        if exact:
            ids = {r[0] for r in exact}
            if len(ids) == 1:
                return all_champs.get(next(iter(ids)))
        partial = con.execute("SELECT DISTINCT champion_id FROM champion_aliases WHERE alias_norm LIKE ?", (needle + "%",)).fetchall()
    ids = {r[0] for r in partial}
    if len(ids) == 1:
        return all_champs.get(next(iter(ids)))
    # fallback for databases created before aliases were populated
    candidates=[]
    for c in all_champs.values():
        variants=[c.get("id", ""), c.get("name", ""), c.get("name_ru", "")]
        if any(normalize_search(v)==needle for v in variants if v): return c
        if any(normalize_search(v).startswith(needle) for v in variants if v): candidates.append(c)
    unique={c["id"]:c for c in candidates}
    return next(iter(unique.values())) if len(unique)==1 else None


def display_champion_name(champion: dict | None, lang: str = "en") -> str:
    if not champion: return ""
    return (champion.get("name_ru") or champion.get("name") or champion.get("id") or "") if lang.lower().startswith("ru") else (champion.get("name") or champion.get("id") or "")


def display_item_name(item_name: str, lang: str = "en") -> str:
    if not lang.lower().startswith("ru"): return item_name
    with connect() as con:
        row=con.execute("SELECT name_ru FROM items WHERE name=?", (item_name,)).fetchone()
    return (row[0] if row and row[0] else item_name)


def get_item(item_name: str) -> Optional[dict]:
    with connect() as con:
        row=con.execute("SELECT * FROM items WHERE name=?", (item_name,)).fetchone()
    return dict(row) if row else None


def update_champion_media(champion_id: str, icon_url: str = "", icon_path: str = "") -> None:
    with connect() as con:
        con.execute("UPDATE champions SET icon_url=COALESCE(NULLIF(?,''),icon_url), icon_path=COALESCE(NULLIF(?,''),icon_path) WHERE id=?", (icon_url, icon_path, champion_id))

def update_item_media(item_name: str, icon_url: str = "", icon_path: str = "") -> None:
    with connect() as con:
        con.execute("UPDATE items SET icon_url=COALESCE(NULLIF(?,''),icon_url), icon_path=COALESCE(NULLIF(?,''),icon_path) WHERE name=?", (icon_url, icon_path, item_name))

def upsert_media_asset(
    asset_key: str, *, source_url: str = "", etag: str = "", last_modified: str = "",
    content_length: int = 0, sha256: str = "", local_path: str = "",
    checked_patch: str = "", last_checked: str = "",
) -> None:
    with connect() as con:
        con.execute(
            """INSERT INTO media_assets(
                asset_key,source_url,etag,last_modified,content_length,sha256,local_path,checked_patch,last_checked
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(asset_key) DO UPDATE SET
                source_url=excluded.source_url, etag=excluded.etag, last_modified=excluded.last_modified,
                content_length=excluded.content_length, sha256=excluded.sha256, local_path=excluded.local_path,
                checked_patch=excluded.checked_patch, last_checked=excluded.last_checked""",
            (asset_key, source_url or "", etag or "", last_modified or "", int(content_length or 0),
             sha256 or "", local_path or "", checked_patch or "", last_checked or ""),
        )


def get_media_asset(asset_key: str) -> Optional[dict]:
    with connect() as con:
        row = con.execute("SELECT * FROM media_assets WHERE asset_key=?", (asset_key,)).fetchone()
    return dict(row) if row else None


def get_matchup_score(champion_id: str, enemy_id: str, preferred_role: str = "") -> float:
    with connect() as con:
        rows=con.execute("SELECT role,score FROM matchups WHERE champion_id=? AND enemy_id=?", (champion_id,enemy_id)).fetchall()
    if not rows:
        return 0.0
    rn = preferred_role.casefold()
    if rn:
        exact = [float(r["score"]) for r in rows if str(r["role"]).casefold() == rn]
        if exact:
            return max(exact, key=abs)
        general = [float(r["score"]) for r in rows if not str(r["role"]).strip()]
        return max(general, key=abs) if general else 0.0
    return max((float(r["score"]) for r in rows), key=abs)

def get_stat(champion_id: str, lane: str, rank_segment: str = "all") -> Optional[dict]:
    with connect() as con: row=con.execute("SELECT * FROM stats WHERE champion_id=? AND lane=? AND rank_segment=?", (champion_id,lane,rank_segment)).fetchone()
    return dict(row) if row else None

def get_item_pool(champion_id: str) -> list[dict]:
    with connect() as con: rows=con.execute("SELECT item_name,category,priority,source FROM item_pools WHERE champion_id=? ORDER BY priority ASC,item_name", (champion_id,)).fetchall()
    return [dict(r) for r in rows]

def get_counter_items(enemy_id: str) -> list[dict]:
    with connect() as con: rows=con.execute("SELECT item_name,reason,source FROM counter_items WHERE enemy_id=? ORDER BY item_name", (enemy_id,)).fetchall()
    return [dict(r) for r in rows]

def get_item_names() -> list[str]:
    with connect() as con: rows=con.execute("SELECT name FROM items ORDER BY LENGTH(name) DESC,name").fetchall()
    return [r[0] for r in rows]


def load_runtime_snapshot() -> dict:
    """Load all read-mostly draft data in one SQLite connection.

    The GUI/engine can keep this snapshot until the updater changes the database,
    avoiding hundreds of tiny SQLite connections during a single recalculation.
    """
    with connect() as con:
        champion_rows = con.execute("SELECT * FROM champions ORDER BY name COLLATE NOCASE").fetchall()
        alias_rows = con.execute("SELECT champion_id,alias_norm FROM champion_aliases").fetchall()
        matchup_rows = con.execute("SELECT champion_id,enemy_id,role,score FROM matchups").fetchall()
        tier_rows = con.execute("SELECT champion_id,role,tier,source,patch FROM champion_tiers").fetchall()
        stat_rows = con.execute("SELECT * FROM stats").fetchall()
        pool_rows = con.execute("SELECT champion_id,item_name,category,priority,source FROM item_pools ORDER BY champion_id,priority ASC,item_name").fetchall()
        counter_rows = con.execute("SELECT enemy_id,item_name,reason,source FROM counter_items ORDER BY enemy_id,item_name").fetchall()
        item_rows = con.execute("SELECT * FROM items").fetchall()

    champions_list: list[dict] = []
    champions_by_id: dict[str, dict] = {}
    for row in champion_rows:
        champ = dict(row)
        champ["roles"] = json.loads(champ.pop("roles_json") or "[]")
        champ["lanes"] = json.loads(champ.pop("lanes_json") or "[]")
        champions_list.append(champ)
        champions_by_id[champ["id"]] = champ

    alias_ids: dict[str, set[str]] = {}
    for champ in champions_list:
        for value in (champ.get("id", ""), champ.get("name", ""), champ.get("name_ru", "")):
            norm = normalize_search(value or "")
            if norm:
                alias_ids.setdefault(norm, set()).add(champ["id"])
    for row in alias_rows:
        norm = str(row["alias_norm"] or "")
        if norm:
            alias_ids.setdefault(norm, set()).add(str(row["champion_id"]))
    champion_aliases = {
        norm: champions_by_id[next(iter(ids))]
        for norm, ids in alias_ids.items()
        if len(ids) == 1 and next(iter(ids)) in champions_by_id
    }

    matchups: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for row in matchup_rows:
        matchups.setdefault((row["champion_id"], row["enemy_id"]), []).append((row["role"], float(row["score"])))

    tiers: dict[tuple[str, str], str] = {}
    # Prefer WildRiftCore when multiple sources happen to contain the same role.
    for row in sorted(tier_rows, key=lambda r: 0 if r["source"] == "wildriftcore.com" else 1):
        key = (row["champion_id"], row["role"])
        tiers.setdefault(key, str(row["tier"] or ""))

    stats = {
        (row["champion_id"], row["lane"], row["rank_segment"]): dict(row)
        for row in stat_rows
    }
    item_pools: dict[str, list[dict]] = {}
    for row in pool_rows:
        item_pools.setdefault(row["champion_id"], []).append(dict(row))
    counter_items: dict[str, list[dict]] = {}
    for row in counter_rows:
        counter_items.setdefault(row["enemy_id"], []).append(dict(row))
    items = {row["name"]: dict(row) for row in item_rows}

    return {
        "champions": champions_list,
        "champions_by_id": champions_by_id,
        "champion_aliases": champion_aliases,
        "champion_alias_ids": alias_ids,
        "matchups": matchups,
        "tiers": tiers,
        "stats": stats,
        "item_pools": item_pools,
        "counter_items": counter_items,
        "items": items,
    }


def resolve_snapshot_champion(snapshot: dict, value: str) -> Optional[dict]:
    """Resolve a champion against an in-memory runtime snapshot."""
    needle = normalize_search(value)
    if not needle:
        return None
    aliases = snapshot.get("champion_aliases", {})
    exact = aliases.get(needle)
    if exact:
        return exact
    by_id = snapshot.get("champions_by_id", {})
    if value in by_id:
        return by_id[value]
    alias_ids = snapshot.get("champion_alias_ids", {})
    candidate_ids: set[str] = set()
    for alias_norm, ids in alias_ids.items():
        if str(alias_norm).startswith(needle):
            candidate_ids.update(ids)
            if len(candidate_ids) > 1:
                break
    if len(candidate_ids) == 1:
        return by_id.get(next(iter(candidate_ids)))
    return None
