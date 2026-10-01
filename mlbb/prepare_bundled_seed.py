from __future__ import annotations

import hashlib
import json
import math
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
        evidence_rows = [
            (str(row[0]), str(row[1]), float(row[2]))
            for row in con.execute(
                "SELECT champion_id,enemy_id,raw_edge FROM matchup_evidence"
            ).fetchall()
        ]
        matchup_sources = [
            str(row[0]) for row in con.execute(
                "SELECT DISTINCT source FROM matchup_evidence ORDER BY source"
            ).fetchall()
        ]
        meta = {
            str(row[0]): str(row[1])
            for row in con.execute(
                "SELECT key,value FROM meta WHERE key IN ("
                "'patch_version','matchup_evidence_source','matchup_direction',"
                "'matchup_sample_window','matchup_patch','matchup_updated_at')"
            ).fetchall()
        }
        sentinel_pairs = (
            ("Paquito", "Karina"),
            ("Minotaur", "Lolita"),
            ("Obsidia", "Benedetta"),
            ("Obsidia", "Aldous"),
        )
        direction_sentinels = {}
        for champion_name, enemy_name in sentinel_pairs:
            row = con.execute(
                "SELECT e.raw_edge FROM matchup_evidence e "
                "JOIN champions c ON c.id=e.champion_id "
                "JOIN champions x ON x.id=e.enemy_id "
                "WHERE lower(c.name)=lower(?) AND lower(x.name)=lower(?) "
                "ORDER BY e.confidence DESC LIMIT 1",
                (champion_name, enemy_name),
            ).fetchone()
            direction_sentinels[f"{champion_name}->{enemy_name}"] = (
                float(row[0]) if row is not None else None
            )

    evidence_by_pair = {(a, b): edge for a, b, edge in evidence_rows}
    mirror_checked = 0
    exact_inverse = 0
    magnitude_residual_sum = 0.0
    seen_pairs = set()
    for (a, b), edge in evidence_by_pair.items():
        pair = tuple(sorted((a, b)))
        if pair in seen_pairs:
            continue
        reverse = evidence_by_pair.get((b, a))
        if reverse is None:
            continue
        seen_pairs.add(pair)
        mirror_checked += 1
        residual = abs(abs(edge) - abs(reverse))
        magnitude_residual_sum += residual
        if edge * reverse < 0 and residual < 1e-12:
            exact_inverse += 1
    mirror_inverse_ratio = (
        exact_inverse / mirror_checked if mirror_checked else 0.0
    )
    mirror_mean_residual = (
        magnitude_residual_sum / mirror_checked if mirror_checked else 0.0
    )

    sign_counts = {}
    for a, _b, edge in evidence_rows:
        bucket = sign_counts.setdefault(a, {"positive": 0, "negative": 0, "total": 0})
        bucket["total"] += 1
        if edge > 0:
            bucket["positive"] += 1
        elif edge < 0:
            bucket["negative"] += 1

    numeric_points = []
    for cid, bucket in sign_counts.items():
        if not cid.isdigit() or bucket["total"] <= 0:
            continue
        numeric_points.append((int(cid), bucket["positive"] / bucket["total"]))
    id_positive_correlation = 0.0
    if len(numeric_points) >= 3:
        xs = [float(x) for x, _y in numeric_points]
        ys = [float(y) for _x, y in numeric_points]
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        covariance = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        if sx > 0 and sy > 0:
            id_positive_correlation = covariance / (sx * sy)

    hero_name_by_id = {str(row["id"]): str(row.get("name") or row["id"]) for row in champions}
    positive_share_top = sorted(
        (
            {
                "id": cid,
                "name": hero_name_by_id.get(cid, cid),
                "positive": bucket["positive"],
                "negative": bucket["negative"],
                "total": bucket["total"],
                "positive_share": bucket["positive"] / bucket["total"] if bucket["total"] else 0.0,
            }
            for cid, bucket in sign_counts.items()
        ),
        key=lambda row: (row["positive_share"], row["positive"]),
        reverse=True,
    )[:10]

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
        "matchup_bidirectional_pairs": mirror_checked,
        "matchup_exact_inverse_ratio": mirror_inverse_ratio,
        "matchup_mean_magnitude_residual": mirror_mean_residual,
        "matchup_id_positive_correlation": id_positive_correlation,
        "matchup_positive_share_top": positive_share_top,
        "matchup_sources": matchup_sources,
        "matchup_meta": meta,
        "matchup_direction_sentinels": direction_sentinels,
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
    if meta.get("patch_version") != "2.2.16":
        raise RuntimeError(
            "Bundled MLBB patch is not the required live Original Server patch 2.2.16: "
            f"{meta.get('patch_version') or 'missing'}"
        )
    if meta.get("matchup_evidence_source") != "mlbb.rone.public.counters.7d":
        raise RuntimeError(
            "Bundled matchup source is not the live 7-day Rone matrix: "
            f"{meta.get('matchup_evidence_source') or 'missing'}"
        )
    if meta.get("matchup_direction") != "main_hero_to_sub_hero":
        raise RuntimeError(
            "Bundled matchup direction contract is missing or inverted: "
            f"{meta.get('matchup_direction') or 'missing'}"
        )
    if meta.get("matchup_sample_window") != "7d":
        raise RuntimeError(
            f"Bundled matchup sample window is not 7d: {meta.get('matchup_sample_window') or 'missing'}"
        )
    if matchup_sources != ["mlbb.rone.public.counters.7d"]:
        raise RuntimeError(
            "Stale matchup evidence sources leaked into the bundled seed: "
            + json.dumps(matchup_sources, ensure_ascii=False)
        )
    bad_sentinels = {
        name: value for name, value in direction_sentinels.items()
        if value is None or value <= 0.0
    }
    if bad_sentinels:
        raise RuntimeError(
            "MLBB 2.2.16 matchup direction sanity-check failed: "
            + json.dumps(bad_sentinels, ensure_ascii=False)
        )
    if len(numeric_points) >= 50 and abs(id_positive_correlation) > 0.95:
        raise RuntimeError(
            "MLBB matchup signs are implausibly correlated with numeric hero IDs: "
            f"correlation={id_positive_correlation:.6f}"
        )
    expected_by_role = {}
    for _cid, role in expected_builds:
        expected_by_role[role] = expected_by_role.get(role, 0) + 1
    actual_by_role = role_distribution
    # Lane assignments are intentionally broader than measured live build coverage.
    # Do not reject a valid live matchup refresh just because Rone's build feed
    # has one canonical build per hero/role subset. Preserve the last-good
    # build rows and require the historically complete floor instead.
    min_total_builds = 160
    if counts["role_builds"] < min_total_builds:
        raise RuntimeError(
            f"Too few role-specific MLBB builds in bundled seed: "
            f"{counts['role_builds']} < {min_total_builds}"
        )
    weak_roles = {}
    for role, expected_count in expected_by_role.items():
        actual_count = int(actual_by_role.get(role, 0))
        required = min(expected_count, {"EXP": 35, "Голд": 18, "Лес": 32, "Мид": 25, "Роум": 32}.get(role, 8))
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
        "source": "Rone Arena 7-day counters + MLBBDex + MLBBHub patch metadata; full offline MLCA seed",
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
