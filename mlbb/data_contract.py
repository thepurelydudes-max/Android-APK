from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
import unicodedata
from typing import Iterable


CANONICAL_LANES = ("exp", "jungle", "mid", "gold", "roam")

LANE_ALIASES = {
    "exp": "exp",
    "exp lane": "exp",
    "offlane": "exp",
    "fighter": "exp",
    "jungle": "jungle",
    "jungler": "jungle",
    "forest": "jungle",
    "mid": "mid",
    "middle": "mid",
    "mid lane": "mid",
    "gold": "gold",
    "gold lane": "gold",
    "marksman": "gold",
    "roam": "roam",
    "roamer": "roam",
    "support": "roam",
}

ROLE_RU_BY_LANE = {
    "exp": "EXP",
    "jungle": "Лес",
    "mid": "Мид",
    "gold": "Голд",
    "roam": "Роум",
}


def slug(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def canonical_lane(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    return LANE_ALIASES.get(text, text if text in CANONICAL_LANES else "")


def canonical_hero_id(source_id: object, name: object = "") -> str:
    """Return a stable internal key without depending on a display name.

    Numeric upstream IDs are preferred because Rone preserves Moonton hero IDs.
    Name slugs are only a fallback for sources that have no numeric identifier.
    """
    raw = str(source_id or "").strip()
    if raw.isdigit():
        return raw
    fallback = slug(name or raw)
    return fallback


def rate_to_percent(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if -1.0 <= numeric <= 1.0:
        return numeric * 100.0
    return numeric


def edge_to_percentage_points(value: object, unit: str) -> float:
    numeric = float(value or 0.0)
    unit = str(unit or "").strip().casefold()
    if unit in {"rate", "fraction", "probability"}:
        return numeric * 100.0
    if unit in {"percentage_points", "pp", "percent_points"}:
        return numeric
    raise ValueError(f"Unsupported matchup edge unit: {unit!r}")


def robust_edge_scale(edges_pp: Iterable[float], percentile: float = 0.95) -> float:
    """Return a robust absolute edge scale derived from the collected matrix.

    The 95th percentile prevents a single noisy matchup from setting the scale.
    The result is never below 0.5 percentage points to avoid over-amplifying
    tiny samples when a source temporarily returns a sparse matrix.
    """
    values = sorted(abs(float(x)) for x in edges_pp if math.isfinite(float(x)))
    if not values:
        return 1.0
    p = min(1.0, max(0.0, float(percentile)))
    index = int(round((len(values) - 1) * p))
    return max(0.5, values[index])


def normalize_edge(edge_pp: float, scale_pp: float) -> float:
    scale = max(0.000001, abs(float(scale_pp)))
    return max(-1.0, min(1.0, float(edge_pp) / scale))


@dataclass(frozen=True)
class LaneEvidence:
    champion_id: str
    lane: str
    evidence_type: str
    source: str
    source_lane_id: str = ""
    rank_segment: str = "all"
    usage_rate: float | None = None
    confidence: float = 1.0
    patch: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class MatchupEvidence:
    champion_id: str
    enemy_id: str
    raw_edge: float
    raw_unit: str
    source: str
    evidence_type: str = "measured"
    role: str = ""
    rank_segment: str = "all"
    sample_window: str = ""
    normalized_edge: float | None = None
    confidence: float = 1.0
    patch: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_matchup_evidence(
    rows: Iterable[MatchupEvidence],
    *,
    percentile: float = 0.95,
) -> tuple[list[MatchupEvidence], float]:
    materialized = list(rows)
    edges_pp = [
        edge_to_percentage_points(row.raw_edge, row.raw_unit)
        for row in materialized
        if str(row.evidence_type or "").startswith("measured")
    ]
    scale_pp = robust_edge_scale(edges_pp, percentile=percentile)
    normalized: list[MatchupEvidence] = []
    for row in materialized:
        edge_pp = edge_to_percentage_points(row.raw_edge, row.raw_unit)
        normalized.append(MatchupEvidence(
            champion_id=row.champion_id,
            enemy_id=row.enemy_id,
            raw_edge=row.raw_edge,
            raw_unit=row.raw_unit,
            source=row.source,
            evidence_type=row.evidence_type,
            role=canonical_lane(row.role),
            rank_segment=row.rank_segment or "all",
            sample_window=row.sample_window,
            normalized_edge=normalize_edge(edge_pp, scale_pp),
            confidence=max(0.0, min(1.0, float(row.confidence))),
            patch=row.patch,
        ))
    return normalized, scale_pp


def choose_supported_lanes(
    evidence: Iterable[LaneEvidence],
    *,
    min_usage_rate: float = 0.10,
) -> dict[str, list[str]]:
    """Resolve lane evidence without pretending assignment metadata is usage.

    - explicit usage evidence wins when present;
    - otherwise source lane assignments are kept as supported assignments;
    - no made-up percentages are generated.
    """
    grouped: dict[str, list[LaneEvidence]] = {}
    for row in evidence:
        lane = canonical_lane(row.lane)
        if not row.champion_id or not lane:
            continue
        grouped.setdefault(row.champion_id, []).append(row)

    out: dict[str, list[str]] = {}
    for champion_id, rows in grouped.items():
        usage_rows = [row for row in rows if row.usage_rate is not None]
        if usage_rows:
            lanes = {
                canonical_lane(row.lane)
                for row in usage_rows
                if float(row.usage_rate or 0.0) >= min_usage_rate
            }
        else:
            lanes = {canonical_lane(row.lane) for row in rows if row.evidence_type == "assignment"}
        out[champion_id] = [lane for lane in CANONICAL_LANES if lane in lanes]
    return out
