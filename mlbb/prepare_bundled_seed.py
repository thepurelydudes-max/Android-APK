from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import db
import updater
from paths import APP_DIR, ASSETS_DIR, RUNTIME_DIR, ensure_initial_data, resolve_media_path


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
        quick_check = str(con.execute("PRAGMA quick_check").fetchone()[0])
        champions = [dict(row) for row in con.execute(
            "SELECT id,name,name_ru,lanes_json,icon_path FROM champions ORDER BY id"
        ).fetchall()]
        items = [dict(row) for row in con.execute(
            "SELECT name,name_ru,icon_path FROM items "
            "WHERE lower(tier)='upgraded' ORDER BY name"
        ).fetchall()]
        counts = {
            "champions": int(con.execute("SELECT COUNT(*) FROM champions").fetchone()[0]),
            "items": int(con.execute("SELECT COUNT(*) FROM items WHERE lower(tier)='upgraded'").fetchone()[0]),
            "stats": int(con.execute("SELECT COUNT(*) FROM stats").fetchone()[0]),
            "matchups": int(con.execute("SELECT COUNT(*) FROM matchups").fetchone()[0]),
            "item_pools": int(con.execute("SELECT COUNT(*) FROM item_pools").fetchone()[0]),
            "counter_items": int(con.execute("SELECT COUNT(*) FROM counter_items").fetchone()[0]),
            "role_builds": int(con.execute("SELECT COUNT(*) FROM role_builds").fetchone()[0]),
            "role_variants": int(con.execute("SELECT COUNT(*) FROM role_build_variants").fetchone()[0]),
            "role_situational": int(con.execute("SELECT COUNT(*) FROM role_build_situational").fetchone()[0]),
            "role_boots": int(con.execute("SELECT COUNT(*) FROM role_build_boots").fetchone()[0]),
        }
        build_keys = {
            (str(row[0]), str(row[1]))
            for row in con.execute("SELECT champion_id,role FROM role_builds").fetchall()
        }

    missing_en = [row["id"] for row in champions if not str(row.get("name") or "").strip()]
    missing_ru = [row["id"] for row in champions if not str(row.get("name_ru") or "").strip()]
    missing_item_ru = [row["name"] for row in items if not str(row.get("name_ru") or "").strip()]

    missing_champion_icons = []
    for row in champions:
        path = str(row.get("icon_path") or "")
        if not path or not _valid_image(resolve_media_path(path)):
            missing_champion_icons.append(str(row.get("id") or "?"))

    missing_item_icons = []
    for row in items:
        path = str(row.get("icon_path") or "")
        if not path or not _valid_image(resolve_media_path(path)):
            missing_item_icons.append(str(row.get("name") or "?"))

    lane_to_role = {
        "exp": "EXP",
        "jungle": "Лес",
        "mid": "Мид",
        "gold": "Голд",
        "roam": "Роум",
    }
    expected_builds = set()
    for row in champions:
        try:
            lanes = json.loads(row.get("lanes_json") or "[]")
        except Exception:
            lanes = []
        for lane in lanes:
            role = lane_to_role.get(str(lane).casefold())
            if role:
                expected_builds.add((str(row["id"]), role))
    missing_role_builds = sorted(
        f"{cid}:{role}" for cid, role in (expected_builds - build_keys)
    )

    role_distribution = {}
    with db.connect() as con:
        for row in con.execute(
            "SELECT role, COUNT(*) AS cnt FROM role_builds GROUP BY role ORDER BY role"
        ).fetchall():
            role_distribution[str(row["role"])] = int(row["cnt"])

    audit = {
        "quick_check": quick_check,
        **counts,
        "missing_en_names": missing_en,
        "missing_ru_names": missing_ru,
        "missing_ru_item_names": missing_item_ru,
        "champion_icons_missing": missing_champion_icons,
        "item_icons_missing": missing_item_icons,
        "expected_role_builds": len(expected_builds),
        "missing_role_builds": missing_role_builds,
        "role_build_distribution": role_distribution,
    }

    print(json.dumps({"seed_audit_preview": audit}, ensure_ascii=False, indent=2), flush=True)

    if quick_check != "ok":
        raise RuntimeError(f"SQLite quick_check failed: {quick_check}")
    if counts["champions"] < 130:
        raise RuntimeError(f"Too few MLBB heroes in bundled seed: {counts['champions']}")
    if counts["items"] < 45:
        raise RuntimeError(f"Too few finished MLBB items in bundled seed: {counts['items']}")
    if counts["matchups"] < 2500:
        raise RuntimeError(f"Too few MLBB matchup rows in bundled seed: {counts['matchups']}")
    expected_by_role = {}
    for _cid, role in expected_builds:
        expected_by_role[role] = expected_by_role.get(role, 0) + 1
    actual_by_role = role_distribution
    min_total_builds = max(80, int(len(expected_builds) * 0.50)) if expected_builds else 80
    if counts["role_builds"] < min_total_builds:
        raise RuntimeError(
            f"Too few role-specific MLBB builds in bundled seed: "
            f"{counts['role_builds']} < {min_total_builds}"
        )
    weak_roles = {}
    for role, expected_count in expected_by_role.items():
        actual_count = int(actual_by_role.get(role, 0))
        required = max(5, int(expected_count * 0.35))
        if actual_count < required:
            weak_roles[role] = {
                "actual": actual_count,
                "expected": expected_count,
                "required": required,
            }
    if weak_roles:
        raise RuntimeError(
            "Role build coverage is too skewed: "
            + json.dumps(weak_roles, ensure_ascii=False)
        )
    if counts["role_variants"] < counts["role_builds"]:
        raise RuntimeError(
            f"Role build variants incomplete: {counts['role_variants']} for {counts['role_builds']} role builds"
        )
    if missing_en or missing_ru or missing_item_ru:
        raise RuntimeError(
            "Bundled localization is incomplete: "
            f"heroes EN={len(missing_en)}, heroes RU={len(missing_ru)}, items RU={len(missing_item_ru)}"
        )
    if missing_champion_icons or missing_item_icons:
        raise RuntimeError(
            "Bundled media is incomplete: "
            f"hero icons={len(missing_champion_icons)}, item icons={len(missing_item_icons)}"
        )
    # Missing source-backed hero+role builds are intentionally retained as
    # audit data instead of being fabricated. The runtime engine has a strict
    # eligibility gate: a hero without a valid build for the selected role is
    # excluded from recommendations on that role. Per-role coverage checks above
    # ensure this cannot silently collapse an entire lane.
    return audit


def main() -> None:
    # Build in an isolated writable runtime. The repository's previous seed is
    # only a bootstrap/fallback; updater refreshes it before anything is copied
    # back into immutable APK assets.
    if RUNTIME_DIR.exists():
        shutil.rmtree(RUNTIME_DIR)
    ensure_initial_data()
    db.init_db()

    summary = updater.update_all(lambda msg: print(msg, flush=True), lang="en")
    audit = _audit()

    seed_data = ASSETS_DIR / "data"
    seed_cache = ASSETS_DIR / "cache"
    seed_data.mkdir(parents=True, exist_ok=True)
    seed_cache.mkdir(parents=True, exist_ok=True)

    runtime_db = APP_DIR / "mobilelegends.db"
    bundled_db = seed_data / "mobilelegends.db"
    if not runtime_db.is_file():
        raise FileNotFoundError(runtime_db)
    shutil.copy2(runtime_db, bundled_db)

    runtime_loc = APP_DIR / "localization_ru.json"
    if runtime_loc.is_file():
        shutil.copy2(runtime_loc, seed_data / "localization_ru.json")

    runtime_cache = RUNTIME_DIR / "cache"
    if runtime_cache.is_dir():
        if seed_cache.exists():
            shutil.rmtree(seed_cache)
        shutil.copytree(runtime_cache, seed_cache)

    # Verify the immutable copy itself, not only the writable runtime.
    with sqlite3.connect(bundled_db) as con:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Bundled mobilelegends.db failed quick_check after copy")

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Rone Arena API + MLBBDex; full offline MLCA seed",
        "patch": db.get_meta("patch_version", ""),
        "last_update": db.get_meta("last_update", ""),
        "database_sha256": hashlib.sha256(bundled_db.read_bytes()).hexdigest(),
        "update_summary": summary,
        "audit": audit,
    }
    (seed_data / "bundled_seed_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
