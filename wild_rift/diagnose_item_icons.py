from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from PIL import Image

from paths import RUNTIME_DIR, ensure_initial_data, resolve_media_path
import db
import updater


def valid_image(path: Path) -> tuple[bool, str]:
    try:
        if not path.is_file():
            return False, "file_missing"
        if path.stat().st_size <= 0:
            return False, "file_empty"
        with Image.open(path) as image:
            image.verify()
        return True, "ok"
    except Exception as exc:
        return False, f"invalid_image: {exc}"


def main() -> int:
    ensure_initial_data()
    db.init_db()

    progress_log: list[str] = []

    def progress(message: str) -> None:
        text = str(message or "")
        progress_log.append(text)
        print(text, flush=True)

    summary = updater.update_all(progress=progress, lang="ru")
    rows = db.item_catalog_rows()

    upgraded = [
        row for row in rows
        if str(row.get("tier") or "").casefold() == "upgraded"
    ]

    missing: list[dict] = []
    for row in upgraded:
        name = str(row.get("name") or "")
        icon_path = str(row.get("icon_path") or "")
        icon_url = str(row.get("icon_url") or "")
        if not icon_path:
            missing.append({
                "name": name,
                "reason": "icon_path_empty",
                "icon_url": icon_url,
                "source": str(row.get("source") or ""),
            })
            continue
        path = resolve_media_path(icon_path)
        good, reason = valid_image(path)
        if not good:
            missing.append({
                "name": name,
                "reason": reason,
                "icon_path": icon_path,
                "resolved_path": str(path),
                "icon_url": icon_url,
                "source": str(row.get("source") or ""),
            })

    with db.connect() as con:
        references = {}
        for table, column in (
            ("item_pools", "item_name"),
            ("counter_items", "item_name"),
            ("role_build_situational", "item_name"),
            ("role_build_boots", "item_name"),
        ):
            refs = [
                str(row[0])
                for row in con.execute(
                    f"SELECT DISTINCT {column} FROM {table} ORDER BY {column}"
                ).fetchall()
            ]
            references[table] = refs

        core_rows = con.execute(
            "SELECT champion_id,role,items_json,boot_name FROM role_builds"
        ).fetchall()

    referenced_names: set[str] = set()
    for refs in references.values():
        referenced_names.update(refs)
    for row in core_rows:
        try:
            referenced_names.update(json.loads(row["items_json"] or "[]"))
        except Exception:
            pass
        if row["boot_name"]:
            referenced_names.add(str(row["boot_name"]))

    by_name = {str(row.get("name") or ""): row for row in rows}
    referenced_missing_catalog = sorted(
        name for name in referenced_names if name and name not in by_name
    )
    referenced_without_icon = sorted(
        name for name in referenced_names
        if name in by_name
        and (
            not str(by_name[name].get("icon_path") or "")
            or not valid_image(resolve_media_path(str(by_name[name].get("icon_path") or "")))[0]
        )
    )

    normalized: dict[str, list[str]] = {}
    import sources
    for row in rows:
        name = str(row.get("name") or "")
        normalized.setdefault(sources.slugish(name), []).append(name)
    duplicate_identities = {
        key: names for key, names in normalized.items()
        if key and len(set(names)) > 1
    }

    special = {
        name: by_name.get(name)
        for name in (
            "Mercury Boots",
            "Mercury Treads",
            "Mercury's Treads",
            "Fiendhunter Bolts",
            "Immortal Boots",
            "Rapid Firecannon",
            "Whispering Circlet",
            "Yun Tal Wildarrows",
            "Kaenic Rookern",
            "Sundered Sky",
        )
    }

    report = {
        "runtime_dir": str(RUNTIME_DIR),
        "update_summary": summary,
        "all_items": len(rows),
        "upgraded_items": len(upgraded),
        "missing_or_invalid_upgraded_icons": missing,
        "referenced_missing_catalog": referenced_missing_catalog,
        "referenced_without_icon": referenced_without_icon,
        "duplicate_normalized_item_identities": duplicate_identities,
        "special_items": special,
    }

    out_dir = Path(os.environ.get("ICON_DIAGNOSTIC_OUTPUT", str(RUNTIME_DIR / "diagnostic")))
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "item_icon_diagnostic.json"
    txt_path = out_dir / "item_icon_diagnostic.txt"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "Wild Rift item icon diagnostic",
        "==============================",
        f"Items total: {len(rows)}",
        f"Upgraded items: {len(upgraded)}",
        f"Missing/invalid upgraded icons: {len(missing)}",
        f"Referenced names missing from catalog: {len(referenced_missing_catalog)}",
        f"Referenced items without valid icon: {len(referenced_without_icon)}",
        "",
        "MISSING / INVALID ICONS:",
    ]
    if missing:
        for row in missing:
            lines.append(
                f"- {row['name']}: {row['reason']} | "
                f"url={row.get('icon_url','')} | source={row.get('source','')}"
            )
    else:
        lines.append("- none")

    lines += ["", "REFERENCED BUT MISSING FROM CATALOG:"]
    lines += [f"- {name}" for name in referenced_missing_catalog] or ["- none"]

    lines += ["", "REFERENCED WITHOUT VALID ICON:"]
    lines += [f"- {name}" for name in referenced_without_icon] or ["- none"]

    lines += ["", "SPECIAL ITEMS:"]
    for name, row in special.items():
        if row is None:
            lines.append(f"- {name}: NOT IN CATALOG")
        else:
            lines.append(
                f"- {name}: tier={row.get('tier','')} "
                f"icon_path={row.get('icon_path','')} "
                f"icon_url={row.get('icon_url','')} "
                f"source={row.get('source','')}"
            )

    lines += ["", "UPDATE WARNINGS:"]
    errs = [str(x) for x in summary.get("errors", [])]
    lines += [f"- {value}" for value in errs] or ["- none"]

    txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + txt_path.read_text(encoding="utf-8"), flush=True)

    # Diagnostic should still upload its report when sources are flaky. It only
    # fails CI when the post-update database itself contains broken item refs.
    return 2 if referenced_missing_catalog or referenced_without_icon else 0


if __name__ == "__main__":
    raise SystemExit(main())
