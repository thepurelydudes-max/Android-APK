from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

import db
import engine
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
        role_build_rows = [
            dict(row) for row in con.execute(
                "SELECT champion_id,role,items_json,boot_name,source "
                "FROM role_builds WHERE source='wildriftcore.com' "
                "ORDER BY champion_id,role"
            ).fetchall()
        ]
        role_variant_rows = [
            dict(row) for row in con.execute(
                "SELECT champion_id,role,variant_name,items_json,trigger_text,"
                "example_enemies_json,example_text,source "
                "FROM role_build_variants WHERE source='wildriftcore.com' "
                "ORDER BY champion_id,role,priority,variant_name"
            ).fetchall()
        ]
        situational_rows = [
            dict(row) for row in con.execute(
                "SELECT champion_id,role,item_name FROM role_build_situational "
                "WHERE source='wildriftcore.com' ORDER BY champion_id,role,priority"
            ).fetchall()
        ]
        opponent_rows = [
            dict(row) for row in con.execute(
                "SELECT champion_id,role,enemy_name,item_name "
                "FROM role_build_opponent_adaptations "
                "WHERE source='wildriftcore.com' ORDER BY champion_id,role,priority"
            ).fetchall()
        ]
        build_cache_rows = int(con.execute(
            "SELECT COUNT(*) FROM build_page_cache "
            "WHERE source='wildriftcore.com'"
        ).fetchone()[0])
        trait_rows = [
            dict(row) for row in con.execute(
                "SELECT champion_id,trait,confidence,evidence_count,mentions "
                "FROM champion_traits WHERE source='wildriftcore.com'"
            ).fetchall()
        ]
        block_counts = {}
        for table, id_col in (
            ("stats", "champion_id"),
            ("champion_tiers", "champion_id"),
            ("matchups", "champion_id"),
            ("item_pools", "champion_id"),
            ("counter_items", "enemy_id"),
            ("role_builds", "champion_id"),
        ):
            block_counts[table] = {
                str(row[0]) for row in con.execute(
                    f"SELECT DISTINCT {id_col} FROM {table}"
                ).fetchall()
            }

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

    champion_ids = {str(row.get("id") or "") for row in champions}
    role_build_keys = {
        (str(row.get("champion_id") or ""), str(row.get("role") or ""))
        for row in role_build_rows
    }
    variant_keys = {
        (str(row.get("champion_id") or ""), str(row.get("role") or ""))
        for row in role_variant_rows
    }
    champions_without_role_build = sorted(
        champion_ids - {cid for cid, _role in role_build_keys}
    )
    roles_without_variants = sorted(
        f"{cid}:{role}" for cid, role in (role_build_keys - variant_keys)
    )
    variant_counts: dict[tuple[str, str], int] = {}
    for row in role_variant_rows:
        key = (
            str(row.get("champion_id") or ""),
            str(row.get("role") or ""),
        )
        variant_counts[key] = variant_counts.get(key, 0) + 1
    roles_with_incomplete_variant_count = sorted(
        f"{cid}:{role}={variant_counts.get((cid, role), 0)}/3"
        for cid, role in role_build_keys
        if variant_counts.get((cid, role), 0) != 3
    )
    situational_keys = {
        (str(row.get("champion_id") or ""), str(row.get("role") or ""))
        for row in situational_rows
    }
    opponent_keys = {
        (str(row.get("champion_id") or ""), str(row.get("role") or ""))
        for row in opponent_rows
    }
    roles_without_situational = sorted(
        f"{cid}:{role}" for cid, role in (role_build_keys - situational_keys)
    )
    roles_without_opponent_adaptations = sorted(
        f"{cid}:{role}" for cid, role in (role_build_keys - opponent_keys)
    )

    incomplete_role_builds = []
    for row in role_build_rows:
        try:
            names = json.loads(row.get("items_json") or "[]")
        except Exception:
            names = []
        if len(names) != 5 or not str(row.get("boot_name") or "").strip():
            incomplete_role_builds.append(
                f"{row.get('champion_id')}:{row.get('role')}"
            )

    incomplete_variants = []
    variants_with_full_items = 0
    variants_metadata_only = 0
    variants_with_examples = 0
    for row in role_variant_rows:
        try:
            names = json.loads(row.get("items_json") or "[]")
        except Exception:
            names = []
        try:
            example_enemies = json.loads(row.get("example_enemies_json") or "[]")
        except Exception:
            example_enemies = []
        if len(names) == 5:
            variants_with_full_items += 1
        elif len(names) == 0:
            variants_metadata_only += 1
        if [x for x in example_enemies if str(x).strip()]:
            variants_with_examples += 1
        # Public WRC exposes every variant name + trigger, but only some pages
        # expose the separate five-item variant row/example draft. 0 and 5 are
        # both valid; 1-4 means a broken parse.
        if (
            len(names) not in {0, 5}
            or not str(row.get("trigger_text") or "").strip()
        ):
            incomplete_variants.append(
                f"{row.get('champion_id')}:{row.get('role')}:{row.get('variant_name')}"
            )

    data_gaps = {
        table: sorted(champion_ids - ids)
        for table, ids in block_counts.items()
    }

    return {
        "champions_total": len(champions),
        "champions_cached": len(champions) - len(missing_champions),
        "champions_missing": missing_champions,
        "items_total": len(items),
        "items_cached": len(items) - len(missing_items),
        "items_missing": missing_items,
        "wrc_role_builds": len(role_build_rows),
        "wrc_role_variants": len(role_variant_rows),
        "wrc_variants_with_full_items": variants_with_full_items,
        "wrc_variants_metadata_only": variants_metadata_only,
        "wrc_variants_with_examples": variants_with_examples,
        "wrc_champion_traits": len(trait_rows),
        "wrc_trait_champions": len({
            str(row.get("champion_id") or "") for row in trait_rows
        }),
        "wrc_situational": len(situational_rows),
        "wrc_opponent_adaptations": len(opponent_rows),
        "wrc_build_cache_pages": build_cache_rows,
        "champions_without_role_build": champions_without_role_build,
        "roles_without_variants": roles_without_variants,
        "roles_with_incomplete_variant_count": roles_with_incomplete_variant_count,
        "roles_without_situational": roles_without_situational,
        "roles_without_opponent_adaptations": roles_without_opponent_adaptations,
        "incomplete_role_builds": incomplete_role_builds,
        "incomplete_variants": incomplete_variants,
        "data_gaps": data_gaps,
    }


def _validate_wrc_formula() -> dict:
    """Replay WRC's own enemy examples through the universal variant selector."""
    snapshot = db.load_runtime_snapshot()
    checked = 0
    correct = 0
    mismatches: list[str] = []

    for (champion_id, role), rows in (
        snapshot.get("role_variants", {}) or {}
    ).items():
        if len(rows) < 3:
            continue
        for expected in rows:
            trigger = str(expected.get("trigger_text") or "")
            examples = [
                str(value).strip()
                for value in (expected.get("example_enemies") or [])
                if str(value).strip()
            ]
            # WRCA currently receives enemies only; ally-dependent WRC examples
            # cannot be fairly replayed without inventing our own allied draft.
            if not examples or "allied" in trigger.casefold():
                continue

            enemy_objs = []
            for name in examples:
                enemy = db.resolve_snapshot_champion(snapshot, name)
                if enemy:
                    enemy_objs.append((enemy, ""))
            if len(enemy_objs) < 2:
                continue

            threat_counts, _by_tag = engine._enemy_threat_profile(
                enemy_objs, snapshot
            )
            chosen = engine._select_role_variant(
                rows, threat_counts, enemy_objs
            )
            checked += 1
            expected_name = str(expected.get("variant_name") or "")
            chosen_name = str((chosen or {}).get("variant_name") or "")
            if chosen_name == expected_name:
                correct += 1
            elif len(mismatches) < 30:
                mismatches.append(
                    f"{champion_id}:{role}: expected={expected_name}; "
                    f"chosen={chosen_name}; trigger={trigger}"
                )

    accuracy = (float(correct) / float(checked)) if checked else 0.0
    return {
        "checked": checked,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "mismatches": mismatches,
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
    formula_validation = _validate_wrc_formula()

    _copy_runtime_into_bundle()

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "github_run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "github_sha": os.environ.get("GITHUB_SHA", ""),
        "patch": summary.get("patch", ""),
        "last_update": summary.get("last_update", ""),
        "audit": audit,
        "wrc_formula_validation": formula_validation,
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

    print(
        f"WildRiftCore: {audit['wrc_role_builds']} role builds, "
        f"{audit['wrc_role_variants']} variants "
        f"({audit['wrc_variants_with_full_items']} full, "
        f"{audit['wrc_variants_metadata_only']} metadata-only), "
        f"{audit['wrc_champion_traits']} trait rows/"
        f"{audit['wrc_trait_champions']} champions, "
        f"{audit['wrc_situational']} situational, "
        f"{audit['wrc_opponent_adaptations']} opponent adaptations, "
        f"{audit['wrc_build_cache_pages']} cached champion pages",
        flush=True,
    )
    for key in (
        "champions_without_role_build",
        "roles_without_variants",
        "roles_with_incomplete_variant_count",
        "roles_without_situational",
        "roles_without_opponent_adaptations",
        "incomplete_role_builds",
        "incomplete_variants",
    ):
        values = audit.get(key) or []
        if values:
            print(f"{key}: " + ", ".join(values[:40]), flush=True)
    for block, values in (audit.get("data_gaps") or {}).items():
        if values:
            print(
                f"Data gap {block}: {len(values)} — " + ", ".join(values[:20]),
                flush=True,
            )

    print(
        "WRC formula validation: "
        f"{formula_validation['correct']}/{formula_validation['checked']} "
        f"({formula_validation['accuracy']:.1%})",
        flush=True,
    )
    if formula_validation["mismatches"]:
        print(
            "Formula mismatches: "
            + " | ".join(formula_validation["mismatches"][:10]),
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

    expected_profiles = int(summary.get("wrc_build_profiles_total") or 0)
    successful_profiles = int(summary.get("wrc_build_pages_success") or 0)
    if expected_profiles <= 0 or successful_profiles != expected_profiles:
        raise RuntimeError(
            "Refusing to package incomplete WildRiftCore pages: "
            f"{successful_profiles}/{expected_profiles}"
        )
    if audit["champions_without_role_build"]:
        raise RuntimeError(
            "Refusing to package champions without WildRiftCore role builds: "
            + ", ".join(audit["champions_without_role_build"])
        )
    # The formula requires exactly three WRC variant rules per role. Variant
    # item rows/examples, generic situational blocks and exact opponent
    # adaptations are optional enrichment because WRC does not expose each of
    # those blocks for every public profile.
    variant_gaps = (
        audit["roles_without_variants"]
        + audit["roles_with_incomplete_variant_count"]
        + audit["incomplete_variants"]
    )
    if audit["incomplete_role_builds"] or variant_gaps:
        raise RuntimeError(
            "Refusing to package incomplete WildRiftCore build/variant blocks: "
            + ", ".join(audit["incomplete_role_builds"] + variant_gaps)
        )
    if (
        formula_validation["checked"] >= 20
        and formula_validation["accuracy"] < 0.90
    ):
        raise RuntimeError(
            "Refusing to package WRC formula regression: "
            f"{formula_validation['correct']}/"
            f"{formula_validation['checked']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
