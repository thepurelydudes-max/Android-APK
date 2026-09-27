from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path("wrc_snapshot")
BUILDS = ROOT / "raw" / "builds"
COUNTERS = ROOT / "raw" / "counters"


def clean(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value.strip("*_ ")


def plain_heading(line: str) -> tuple[int, str] | None:
    m = re.match(r"^(#{1,6})\s+(.+)$", str(line or "").strip())
    if not m:
        return None
    return len(m.group(1)), clean(m.group(2))


def next_nonempty(lines: list[str], start: int) -> str:
    for line in lines[start:]:
        value = clean(re.sub(r"^#{1,6}\s+", "", line))
        if value:
            return value
    return ""


def extract_build_inventory(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = [line.rstrip() for line in text.splitlines()]
    headings = []
    role_sections = []
    variant_candidates = []
    when_to_pick = []
    example_explanations = []
    situational_lines = []
    opponent_lines = []

    current_role = ""
    section = ""
    for i, raw in enumerate(lines):
        h = plain_heading(raw)
        if h:
            level, title = h
            headings.append({"level": level, "title": title})
            folded = title.casefold()
            if "recommended build" in folded:
                current_role = title
                role_sections.append(title)
                section = "role"
            elif folded == "situational adaptations":
                section = "situational"
            elif folded == "adaptations by opponent":
                section = "opponent"
            elif folded.startswith("power vs."):
                section = "other"
            elif (
                current_role
                and level in {3, 4, 5}
                and folded not in {
                    "situational adaptations",
                    "adaptations by opponent",
                    "power vs. the roster",
                }
                and not folded.startswith(("recommended build", "core build"))
            ):
                variant_candidates.append({
                    "role_section": current_role,
                    "level": level,
                    "title": title,
                })

        folded_line = clean(raw).casefold()
        if folded_line == "when to pick it":
            value = next_nonempty(lines, i + 1)
            if value:
                when_to_pick.append({
                    "role_section": current_role,
                    "value": value,
                })
        elif folded_line == "example enemy draft":
            explanation = ""
            for candidate in lines[i + 1:i + 12]:
                plain = clean(candidate)
                if not plain:
                    continue
                if "Image:" in plain or "/champions/" in plain:
                    continue
                if plain.casefold().startswith("open this draft"):
                    break
                if plain_heading(candidate):
                    break
                explanation = clean(re.sub(r"\[[^\]]+\]\([^)]*\)", "", plain))
                if explanation:
                    break
            example_explanations.append({
                "role_section": current_role,
                "value": explanation,
            })
        elif section == "situational":
            plain = clean(raw)
            if plain and not plain.startswith("#") and (
                "against " in plain.casefold() or " vs " in plain.casefold()
            ):
                situational_lines.append({
                    "role_section": current_role,
                    "value": plain,
                })
        elif section == "opponent":
            plain = clean(raw)
            if (
                plain
                and "›" in plain
                and not plain.casefold().startswith("your hardest matchups")
            ):
                opponent_lines.append({
                    "role_section": current_role,
                    "value": plain,
                })

    return {
        "slug": path.stem,
        "role_sections": role_sections,
        "variant_candidates": variant_candidates,
        "when_to_pick": when_to_pick,
        "example_explanations": example_explanations,
        "situational_lines": situational_lines,
        "opponent_lines": opponent_lines,
        "markers": {
            "has_when_to_pick": bool(when_to_pick),
            "has_example_enemy_draft": "example enemy draft" in text.casefold(),
            "has_situational_adaptations": "situational adaptations" in text.casefold(),
            "has_adaptations_by_opponent": "adaptations by opponent" in text.casefold(),
        },
    }


def extract_counter_inventory(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    folded = text.casefold()
    return {
        "slug": path.stem,
        "markers": {
            "has_edge": "edge" in folded,
            "has_best_picks": "best picks" in folded,
            "has_do_not_pick": "do not pick" in folded,
            "has_key_item": "key item" in folded,
        },
    }


def main() -> int:
    if not BUILDS.is_dir():
        raise FileNotFoundError(BUILDS)

    build_rows = [extract_build_inventory(path) for path in sorted(BUILDS.glob("*.md"))]
    counter_rows = [
        extract_counter_inventory(path) for path in sorted(COUNTERS.glob("*.md"))
    ]

    variant_titles = Counter()
    when_values = Counter()
    for row in build_rows:
        for item in row["variant_candidates"]:
            variant_titles[item["title"]] += 1
        for item in row["when_to_pick"]:
            when_values[item["value"]] += 1

    report = {
        "build_pages": len(build_rows),
        "counter_pages": len(counter_rows),
        "totals": {
            "role_sections": sum(len(row["role_sections"]) for row in build_rows),
            "variant_candidates": sum(len(row["variant_candidates"]) for row in build_rows),
            "when_to_pick_blocks": sum(len(row["when_to_pick"]) for row in build_rows),
            "example_blocks": sum(len(row["example_explanations"]) for row in build_rows),
            "situational_lines": sum(len(row["situational_lines"]) for row in build_rows),
            "opponent_adaptation_lines": sum(len(row["opponent_lines"]) for row in build_rows),
        },
        "unique_variant_titles": [
            {"value": value, "count": count}
            for value, count in variant_titles.most_common()
        ],
        "unique_when_to_pick": [
            {"value": value, "count": count}
            for value, count in when_values.most_common()
        ],
        "pages_missing_markers": {
            key: [
                row["slug"] for row in build_rows
                if not row["markers"].get(key)
            ]
            for key in (
                "has_when_to_pick",
                "has_example_enemy_draft",
                "has_situational_adaptations",
                "has_adaptations_by_opponent",
            )
        },
        "counter_pages_missing_markers": {
            key: [
                row["slug"] for row in counter_rows
                if not row["markers"].get(key)
            ]
            for key in ("has_edge", "has_best_picks", "has_do_not_pick", "has_key_item")
        },
        "champions": build_rows,
        "counters": counter_rows,
    }

    out = ROOT / "inventory.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "build_pages": report["build_pages"],
        "counter_pages": report["counter_pages"],
        "totals": report["totals"],
        "unique_variant_titles": len(report["unique_variant_titles"]),
        "unique_when_to_pick": len(report["unique_when_to_pick"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
