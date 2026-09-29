from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from data_contract import (
    CANONICAL_LANES,
    LaneEvidence,
    MatchupEvidence,
    canonical_hero_id,
    canonical_lane,
    choose_supported_lanes,
    normalize_matchup_evidence,
)
from moonton_gms import (
    MOONTON_GMS_SOURCE_NAME,
    MOONTON_GMS_SOURCE_URL,
    parse_gms_source,
)
from sources import (
    Net,
    RONE_PUBLIC_HEROES,
    RONE_HERO_RANK,
    RONE_EQUIPMENT_EXPANDED,
    parse_rone_public_heroes,
    parse_rone_rank_payload,
    parse_rone_equipment,
    parse_rone_build_variants,
)

RONE_ACADEMY_HERO_FILTERS = "https://arena.rone.dev/api/academy/heroes"
RONE_ACADEMY_HERO_COUNTERS = "https://arena.rone.dev/api/academy/heroes/{hero_id}/counters"
RONE_META_VERSION = "https://arena.rone.dev/api/academy/meta/version"


def _records(payload: dict) -> list[dict]:
    data = (payload or {}).get("data") or {}
    rows = data.get("records") or [] if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def _row_data(record: dict) -> dict:
    data = (record or {}).get("data")
    return data if isinstance(data, dict) else {}


def parse_lane_filter_evidence(
    payload: dict,
    lane: str,
    *,
    source: str = "rone.academy.hero_filter",
) -> list[LaneEvidence]:
    lane = canonical_lane(lane)
    if not lane:
        return []
    out: list[LaneEvidence] = []
    seen: set[str] = set()
    for record in _records(payload):
        row = _row_data(record)
        hero = row.get("hero") if isinstance(row.get("hero"), dict) else {}
        hero_data = hero.get("data") if isinstance(hero.get("data"), dict) else {}
        champion_id = canonical_hero_id(
            row.get("hero_id") or row.get("heroid"),
            hero_data.get("name") or row.get("hero_name"),
        )
        if not champion_id or champion_id in seen:
            continue
        seen.add(champion_id)
        out.append(LaneEvidence(
            champion_id=champion_id,
            lane=lane,
            evidence_type="assignment",
            source=source,
            confidence=1.0,
        ))
    return out


def metadata_lane_evidence(heroes: list[dict]) -> list[LaneEvidence]:
    out: list[LaneEvidence] = []
    for hero in heroes:
        champion_id = canonical_hero_id(hero.get("id"), hero.get("name"))
        lane_id_map = {
            str(source_id): canonical_lane(lane)
            for source_id, lane in (hero.get("lane_id_map") or {}).items()
            if canonical_lane(lane)
        }
        inverse = {lane: source_id for source_id, lane in lane_id_map.items()}
        for raw_lane in hero.get("lanes") or []:
            lane = canonical_lane(raw_lane)
            if lane:
                out.append(LaneEvidence(
                    champion_id=champion_id,
                    lane=lane,
                    evidence_type="assignment",
                    source="rone.heroes.metadata",
                    source_lane_id=inverse.get(lane, ""),
                    confidence=0.9,
                ))
    return out


def parse_counter_evidence(
    payload: dict,
    target_id: str,
    *,
    rank_segment: str,
    source: str = "rone.academy",
) -> list[MatchupEvidence]:
    """Fallback parser for signed Academy counter evidence.

    Rone's increase_win_rate already carries direction. Preserve that signed
    measurement exactly and do not synthesize a reverse row; the reverse
    target is collected from its own hero page.
    """
    edges: dict[tuple[str, str], MatchupEvidence] = {}
    for record in _records(payload):
        row = _row_data(record)
        resolved_target = canonical_hero_id(row.get("main_heroid") or row.get("heroid") or target_id)
        if not resolved_target:
            continue
        for key in ("sub_hero", "sub_hero_last"):
            for sub in row.get(key) or []:
                if not isinstance(sub, dict):
                    continue
                candidate = canonical_hero_id(sub.get("heroid"))
                if not candidate or candidate == resolved_target:
                    continue
                try:
                    edge = float(sub.get("increase_win_rate") or 0.0)
                except (TypeError, ValueError):
                    continue
                if edge == 0:
                    continue
                item = MatchupEvidence(
                    champion_id=candidate,
                    enemy_id=resolved_target,
                    raw_edge=edge,
                    raw_unit="fraction",
                    source=source,
                    evidence_type=f"measured:{key}",
                    rank_segment=rank_segment,
                    sample_window="academy-current",
                    confidence=1.0,
                )
                old = edges.get((candidate, resolved_target))
                if old is None or abs(item.raw_edge) > abs(old.raw_edge):
                    edges[(candidate, resolved_target)] = item
    return list(edges.values())

def fetch_patch(net: Net) -> str:
    try:
        for record in _records(net.get(f"{RONE_META_VERSION}?size=10&index=1&order=desc&lang=en").json()):
            row = _row_data(record)
            value = str(row.get("game_version") or "").strip()
            if value:
                return value
    except Exception:
        pass
    return ""


def _direction_diagnostic(rows: list[MatchupEvidence]) -> dict:
    by_pair = {(row.champion_id, row.enemy_id): row for row in rows}
    checked = 0
    opposite = 0
    residuals: list[float] = []
    seen: set[frozenset[str]] = set()
    for (a, b), row in by_pair.items():
        pair = frozenset((a, b))
        if pair in seen:
            continue
        reverse = by_pair.get((b, a))
        if reverse is None:
            continue
        seen.add(pair)
        checked += 1
        if row.raw_edge * reverse.raw_edge < 0:
            opposite += 1
        residuals.append(abs(abs(row.raw_edge) - abs(reverse.raw_edge)))
    return {
        "bidirectional_pairs_checked": checked,
        "opposite_sign_pairs": opposite,
        "opposite_sign_ratio": (opposite / checked) if checked else None,
        "mean_abs_magnitude_residual_fraction": (
            sum(residuals) / len(residuals) if residuals else None
        ),
    }


def collect_snapshot(*, rank: str = "all", days: int = 7, emit=print) -> dict:
    net = Net(delay=0.08)
    patch = fetch_patch(net)
    errors: list[str] = []

    emit("1/5 heroes + lane assignments")
    hero_payload = net.get(f"{RONE_PUBLIC_HEROES}?size=300&index=1&order=asc&lang=en").json()
    heroes = parse_rone_public_heroes(hero_payload)
    hero_ids = {
        canonical_hero_id(row.get("id"), row.get("name")): row
        for row in heroes
        if canonical_hero_id(row.get("id"), row.get("name"))
    }

    lanes: list[LaneEvidence] = []
    lane_filter_ok = 0
    for lane in CANONICAL_LANES:
        try:
            payload = net.get(
                f"{RONE_ACADEMY_HERO_FILTERS}?lane={lane}&size=300&index=1&order=asc&lang=en"
            ).json()
            rows = parse_lane_filter_evidence(payload, lane)
            if rows:
                lane_filter_ok += 1
                lanes.extend(rows)
        except Exception as exc:
            errors.append(f"lane filter {lane}: {exc}")
    if lane_filter_ok < len(CANONICAL_LANES):
        # Metadata is a fallback only; it never invents a usage percentage.
        existing = {(row.champion_id, row.lane) for row in lanes}
        for row in metadata_lane_evidence(heroes):
            if (row.champion_id, row.lane) not in existing:
                lanes.append(row)

    emit("2/5 direct GMS stats + matchup evidence")
    rank_rows: list[dict] = []
    matchups: list[MatchupEvidence] = []
    matchup_source = ""
    gms_heroes: list[dict] = []
    try:
        gms_payload = net.get(MOONTON_GMS_SOURCE_URL).json()
        parsed_gms = parse_gms_source(gms_payload, rank_segment="source")
        gms_heroes = list(parsed_gms["heroes"])
        rank_rows = list(parsed_gms["stats"])
        matchups = list(parsed_gms["matchups"])
        matchup_source = MOONTON_GMS_SOURCE_NAME
    except Exception as exc:
        errors.append(f"direct GMS: {exc}")

    if not rank_rows:
        try:
            rank_payload = net.get(
                f"{RONE_HERO_RANK}?days={int(days)}&rank={rank}&size=300&index=1&lang=en"
            ).json()
            rank_rows = parse_rone_rank_payload(rank_payload)
        except Exception as exc:
            errors.append(f"Rone rank stats: {exc}")

    # Detailed per-hero Academy counters are only a fallback.  Normal operation
    # uses the one-shot GMS source and therefore avoids hundreds of requests.
    if not matchups:
        matchup_source = "rone.academy.fallback"
        for index, champion_id in enumerate(hero_ids, 1):
            if index == 1 or index % 25 == 0 or index == len(hero_ids):
                emit(f"fallback counters {index}/{len(hero_ids)}")
            try:
                payload = net.get(
                    RONE_ACADEMY_HERO_COUNTERS.format(hero_id=champion_id)
                    + f"?rank={rank}&size=50&index=1&lang=en"
                ).json()
                matchups.extend(parse_counter_evidence(
                    payload, champion_id, rank_segment=rank, source=matchup_source
                ))
            except Exception as exc:
                errors.append(f"counter {champion_id}: {exc}")

    emit("3/5 items")
    equipment_by_id: dict[int, str] = {}
    try:
        item_payload = net.get(
            f"{RONE_EQUIPMENT_EXPANDED}?size=300&index=1&lang=en"
        ).json()
        items, equipment_by_id = parse_rone_equipment(item_payload)
    except Exception as exc:
        items = []
        errors.append(f"equipment: {exc}")

    emit("4/5 exact API lane build coverage")
    supported_lanes = choose_supported_lanes(lanes)
    build_coverage: list[dict] = []
    if equipment_by_id:
        for index, (champion_id, hero_lanes) in enumerate(supported_lanes.items(), 1):
            if index == 1 or index % 20 == 0 or index == len(supported_lanes):
                emit(f"build coverage {index}/{len(supported_lanes)}")
            for lane in hero_lanes:
                url = (
                    "https://arena.rone.dev/api/academy/heroes/"
                    f"{champion_id}/builds?rank={rank}&lane={lane}&size=100&index=1&lang=en"
                )
                try:
                    variants = parse_rone_build_variants(net.get(url).json(), equipment_by_id)
                except Exception as exc:
                    variants = []
                    errors.append(f"build {champion_id}/{lane}: {exc}")
                lengths = [
                    len(list(row.get("items") or []))
                    for row in variants
                    if isinstance(row, dict)
                ]
                build_coverage.append({
                    "champion_id": champion_id,
                    "lane": lane,
                    "variant_count": len(variants),
                    "max_item_count": max(lengths, default=0),
                    "has_measured_core": any(length >= 3 for length in lengths),
                    "has_full_six": any(length >= 6 for length in lengths),
                    "source_url": url,
                })

    emit("5/5 normalize + diagnostics")
    dedup: dict[tuple[str, str, str, str], MatchupEvidence] = {}
    for row in matchups:
        key = (row.champion_id, row.enemy_id, row.rank_segment, row.source)
        previous = dedup.get(key)
        if previous is None or abs(row.raw_edge) > abs(previous.raw_edge):
            dedup[key] = row
    normalized, edge_scale_pp = normalize_matchup_evidence(dedup.values())
    direction = _direction_diagnostic(list(dedup.values()))

    lane_counts = {lane: 0 for lane in CANONICAL_LANES}
    for values in supported_lanes.values():
        for lane in values:
            lane_counts[lane] += 1

    metadata_pairs = {
        (champion_id, canonical_lane(lane))
        for champion_id, row in hero_ids.items()
        for lane in (row.get("lanes") or [])
        if canonical_lane(lane)
    }
    assigned_pairs = {
        (champion_id, lane)
        for champion_id, values in supported_lanes.items()
        for lane in values
    }

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "contract_version": 1,
        "source": {
            "matchups": matchup_source,
            "academy": "Rone Arena API",
            "rank_filter": rank,
            "days": int(days),
            "patch": patch,
        },
        "counts": {
            "heroes": len(hero_ids),
            "gms_heroes": len(gms_heroes),
            "rank_rows": len(rank_rows),
            "lane_evidence": len(lanes),
            "supported_hero_lanes": len(assigned_pairs),
            "metadata_hero_lanes": len(metadata_pairs),
            "metadata_only_lane_pairs": len(metadata_pairs - assigned_pairs),
            "academy_only_lane_pairs": len(assigned_pairs - metadata_pairs),
            "items": len(items),
            "exact_lane_build_rows": len(build_coverage),
            "exact_lane_builds_with_core": sum(1 for row in build_coverage if row["has_measured_core"]),
            "exact_lane_builds_missing": sum(1 for row in build_coverage if not row["has_measured_core"]),
            "exact_lane_full_six": sum(1 for row in build_coverage if row["has_full_six"]),
            "matchup_evidence": len(normalized),
            "errors": len(errors),
        },
        "lane_counts": lane_counts,
        "edge_scale_pp_p95": edge_scale_pp,
        "direction_validation": direction,
        "metadata_only_lane_pairs": sorted(f"{cid}:{lane}" for cid, lane in metadata_pairs - assigned_pairs),
        "academy_only_lane_pairs": sorted(f"{cid}:{lane}" for cid, lane in assigned_pairs - metadata_pairs),
        "heroes": [
            {
                "id": champion_id,
                "name": str(row.get("name") or ""),
                "roles": list(row.get("roles") or []),
                "metadata_lanes": [canonical_lane(x) for x in (row.get("lanes") or []) if canonical_lane(x)],
                "supported_lanes": supported_lanes.get(champion_id, []),
            }
            for champion_id, row in hero_ids.items()
        ],
        "rank_stats": rank_rows,
        "lane_evidence": [row.to_dict() for row in lanes],
        "build_coverage": build_coverage,
        "missing_exact_lane_builds": [
            f"{row['champion_id']}:{row['lane']}"
            for row in build_coverage
            if not row["has_measured_core"]
        ],
        "matchup_evidence": [row.to_dict() for row in normalized],
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an API-first diagnostic MLBB snapshot.")
    parser.add_argument("--rank", default="all", choices=["all", "epic", "legend", "mythic", "honor", "glory"])
    parser.add_argument("--days", type=int, default=7, choices=[1, 3, 7, 15, 30])
    parser.add_argument("--output", default="mlbb_snapshot.json")
    args = parser.parse_args()

    snapshot = collect_snapshot(rank=args.rank, days=args.days)
    target = Path(args.output)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(target),
        **snapshot["counts"],
        "edge_scale_pp_p95": snapshot["edge_scale_pp_p95"],
        "direction_validation": snapshot["direction_validation"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
