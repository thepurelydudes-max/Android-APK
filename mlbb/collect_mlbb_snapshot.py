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
from sources import (
    Net,
    RONE_PUBLIC_HEROES,
    RONE_HERO_RANK,
    parse_rone_public_heroes,
    parse_rone_rank_payload,
)

RONE_ACADEMY_HERO_LANE = "https://arena.rone.dev/api/academy/heroes/{hero_id}/lane"
RONE_ACADEMY_HERO_COUNTERS = "https://arena.rone.dev/api/academy/heroes/{hero_id}/counters"
RONE_META_VERSION = "https://arena.rone.dev/api/academy/meta/version"


def _records(payload: dict) -> list[dict]:
    data = (payload or {}).get("data") or {}
    records = data.get("records") or [] if isinstance(data, dict) else []
    return [row for row in records if isinstance(row, dict)]


def _row_data(record: dict) -> dict:
    data = (record or {}).get("data")
    return data if isinstance(data, dict) else {}


def parse_lane_evidence(payload: dict, champion_id: str, source: str = "rone.academy") -> list[LaneEvidence]:
    out: list[LaneEvidence] = []
    seen: set[tuple[str, str]] = set()
    for record in _records(payload):
        row = _row_data(record)
        hero = row.get("hero") if isinstance(row.get("hero"), dict) else {}
        hero_data = hero.get("data") if isinstance(hero.get("data"), dict) else {}
        roads = hero_data.get("roadsort") or row.get("roadsort") or []
        for road in roads:
            if not isinstance(road, dict):
                continue
            road_data = road.get("data") if isinstance(road.get("data"), dict) else road
            lane = canonical_lane(
                road_data.get("road_sort_title")
                or road.get("caption")
                or road_data.get("title")
            )
            lane_id = str(road_data.get("road_sort_id") or "").strip()
            if not lane:
                continue
            key = (champion_id, lane)
            if key in seen:
                continue
            seen.add(key)
            out.append(LaneEvidence(
                champion_id=champion_id,
                lane=lane,
                evidence_type="assignment",
                source=source,
                source_lane_id=lane_id,
                confidence=1.0,
            ))
    return out


def parse_counter_evidence(
    payload: dict,
    target_id: str,
    *,
    rank_segment: str,
    source: str = "rone.academy",
) -> list[MatchupEvidence]:
    """Preserve Rone/Moonton increase_win_rate as a raw fractional edge.

    Positive rows are oriented candidate -> target. Reverse edges are emitted
    symmetrically so the matrix contract is deterministic even when only one
    side of a pair is present in a response.
    """
    edges: dict[tuple[str, str], MatchupEvidence] = {}
    for record in _records(payload):
        row = _row_data(record)
        resolved_target = canonical_hero_id(row.get("main_heroid") or row.get("heroid") or target_id)
        if not resolved_target:
            continue
        for key, sign in (("sub_hero", 1.0), ("sub_hero_last", -1.0)):
            for sub in row.get(key) or []:
                if not isinstance(sub, dict):
                    continue
                candidate = canonical_hero_id(sub.get("heroid"))
                if not candidate or candidate == resolved_target:
                    continue
                try:
                    delta = abs(float(sub.get("increase_win_rate") or 0.0))
                except (TypeError, ValueError):
                    continue
                if delta <= 0:
                    continue
                edge = sign * delta
                evidence = MatchupEvidence(
                    champion_id=candidate,
                    enemy_id=resolved_target,
                    raw_edge=edge,
                    raw_unit="fraction",
                    source=source,
                    evidence_type="measured",
                    rank_segment=rank_segment,
                    sample_window="academy-current",
                    confidence=1.0,
                )
                reverse = MatchupEvidence(
                    champion_id=resolved_target,
                    enemy_id=candidate,
                    raw_edge=-edge,
                    raw_unit="fraction",
                    source=source,
                    evidence_type="measured",
                    rank_segment=rank_segment,
                    sample_window="academy-current",
                    confidence=1.0,
                )
                edges[(candidate, resolved_target)] = evidence
                edges[(resolved_target, candidate)] = reverse
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


def collect_snapshot(*, rank: str = "all", days: int = 7, emit=print) -> dict:
    net = Net()
    patch = fetch_patch(net)

    emit("1/4 heroes")
    hero_payload = net.get(f"{RONE_PUBLIC_HEROES}?size=300&index=1&order=asc&lang=en").json()
    heroes = parse_rone_public_heroes(hero_payload)
    hero_ids = {
        canonical_hero_id(row.get("id"), row.get("name")): row
        for row in heroes
        if canonical_hero_id(row.get("id"), row.get("name"))
    }

    emit("2/4 rank stats")
    rank_rows = []
    try:
        rank_payload = net.get(
            f"{RONE_HERO_RANK}?days={int(days)}&rank={rank}&size=300&index=1&lang=en"
        ).json()
        rank_rows = parse_rone_rank_payload(rank_payload)
    except Exception as exc:
        emit(f"rank stats warning: {exc}")

    emit("3/4 lane assignments + raw matchup evidence")
    lanes: list[LaneEvidence] = []
    matchups: list[MatchupEvidence] = []
    errors: list[str] = []
    for index, champion_id in enumerate(hero_ids, 1):
        if index == 1 or index % 20 == 0 or index == len(hero_ids):
            emit(f"hero evidence {index}/{len(hero_ids)}")
        try:
            payload = net.get(
                RONE_ACADEMY_HERO_LANE.format(hero_id=champion_id)
                + "?size=20&index=1&lang=en"
            ).json()
            rows = parse_lane_evidence(payload, champion_id)
            for row in rows:
                lanes.append(LaneEvidence(**{**asdict(row), "patch": patch}))
        except Exception as exc:
            errors.append(f"lane {champion_id}: {exc}")
        try:
            payload = net.get(
                RONE_ACADEMY_HERO_COUNTERS.format(hero_id=champion_id)
                + f"?rank={rank}&size=50&index=1&lang=en"
            ).json()
            rows = parse_counter_evidence(payload, champion_id, rank_segment=rank)
            for row in rows:
                matchups.append(MatchupEvidence(**{**asdict(row), "patch": patch}))
        except Exception as exc:
            errors.append(f"counter {champion_id}: {exc}")

    emit("4/4 normalize")
    # Deduplicate evidence collected from both sides of the same pair.
    dedup: dict[tuple[str, str, str, str], MatchupEvidence] = {}
    for row in matchups:
        key = (row.champion_id, row.enemy_id, row.rank_segment, row.source)
        previous = dedup.get(key)
        if previous is None or abs(row.raw_edge) > abs(previous.raw_edge):
            dedup[key] = row
    normalized, edge_scale_pp = normalize_matchup_evidence(dedup.values())
    supported_lanes = choose_supported_lanes(lanes)

    lane_counts = {lane: 0 for lane in CANONICAL_LANES}
    for values in supported_lanes.values():
        for lane in values:
            lane_counts[lane] += 1

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "contract_version": 1,
        "source": {
            "name": "Rone Arena API",
            "kind": "public community API preserving upstream MLBB shapes",
            "rank": rank,
            "days": int(days),
            "patch": patch,
        },
        "counts": {
            "heroes": len(hero_ids),
            "rank_rows": len(rank_rows),
            "lane_evidence": len(lanes),
            "supported_hero_lanes": sum(len(v) for v in supported_lanes.values()),
            "matchup_evidence": len(normalized),
            "errors": len(errors),
        },
        "lane_counts": lane_counts,
        "edge_scale_pp_p95": edge_scale_pp,
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
    print(json.dumps({"output": str(target), **snapshot["counts"], "edge_scale_pp_p95": snapshot["edge_scale_pp_p95"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
