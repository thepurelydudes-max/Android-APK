from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


DEFAULT_WEIGHTS = {
    "matchup": 0.60,
    "coverage": 0.20,
    "tier": 0.15,
    "winrate": 0.05,
}


@dataclass(frozen=True)
class DraftEdge:
    """One candidate-vs-enemy matrix cell after role inference."""

    enemy_id: str
    enemy_name: str
    enemy_role: str
    edge: float
    weight: float = 1.0


class DraftMatrixEngine:
    """Pure scoring core for WRCA draft recommendations.

    WildRiftCore matchup values are expected on the symmetric -3..+3 scale:
    +3 means the candidate strongly counters the enemy, -3 means the inverse.
    The caller remains responsible for role filtering and retrieving current
    tier/win-rate data; this class only turns one matrix row into deterministic
    0..100 components and a single final score.
    """

    def __init__(self, weights: Mapping[str, float] | None = None):
        merged = dict(DEFAULT_WEIGHTS)
        if weights:
            merged.update({str(k): float(v) for k, v in weights.items()})
        total = sum(merged.values())
        if total <= 0:
            raise ValueError("DraftMatrixEngine weights must have a positive sum")
        # Normalize so future config edits cannot silently move the score away
        # from the documented 0..100 scale.
        self.weights = {key: value / total for key, value in merged.items()}

    @staticmethod
    def clamp_edge(value: float) -> float:
        return max(-3.0, min(3.0, float(value)))

    @staticmethod
    def lane_weight(my_role: str, enemy_role: str) -> float:
        """Give the likely direct lane opponent more influence, never exclusivity."""
        if not enemy_role:
            return 1.0
        if my_role in {"Барон", "Мид"}:
            return 2.0 if enemy_role == my_role else 1.0
        if my_role == "Лес":
            return 1.5 if enemy_role == "Лес" else 1.0
        if my_role in {"ADC", "Саппорт"}:
            return 1.5 if enemy_role in {"ADC", "Саппорт"} else 1.0
        return 1.0

    def analyze_row(self, edges: Iterable[DraftEdge]) -> dict:
        rows = list(edges)
        if not rows:
            return {
                "matchup_score": 50.0,
                "coverage_score": 0.0,
                "coverage_count": 0,
                "coverage_total": 0,
                "positive_strength": 0.0,
                "negative_strength": 0.0,
                "hard_counters": 0,
                "avg_edge": 0.0,
                "weighted_edge_sum": 0.0,
                "positive_weight": 0.0,
                "total_weight": 0.0,
                "positive": [],
                "negative": [],
                "neutral": [],
                "direct_lane_edges": [],
                "mirror_edge": None,
                "enemy_roles": {},
                "matrix_row": [],
            }

        weighted_edge_sum = 0.0
        positive_weight = 0.0
        total_weight = 0.0
        positive_strength = 0.0
        negative_strength = 0.0
        hard_counters = 0
        positives: list[str] = []
        negatives: list[str] = []
        neutral: list[str] = []
        direct_lane_edges: list[float] = []
        mirror_edge = None
        enemy_roles: dict[str, str] = {}
        matrix_row: list[dict] = []

        for row in rows:
            edge = self.clamp_edge(row.edge)
            weight = max(0.0, float(row.weight))
            total_weight += weight
            weighted_edge_sum += (edge / 3.0) * weight
            enemy_roles[row.enemy_id] = row.enemy_role

            if weight > 1.0:
                direct_lane_edges.append(edge)

            if edge > 0:
                positives.append(row.enemy_name)
                positive_weight += weight
                positive_strength += edge * weight
                if edge >= 2.0:
                    hard_counters += 1
            elif edge < 0:
                negatives.append(row.enemy_name)
                negative_strength += abs(edge) * weight
            else:
                neutral.append(row.enemy_name)

            matrix_row.append({
                "enemy_id": row.enemy_id,
                "enemy_name": row.enemy_name,
                "enemy_role": row.enemy_role,
                "edge": edge,
                "weight": weight,
            })

        avg_edge = weighted_edge_sum / total_weight if total_weight > 0 else 0.0
        avg_edge = max(-1.0, min(1.0, avg_edge))
        matchup_score = 50.0 + 50.0 * avg_edge

        # Coverage answers a different question from matchup strength:
        # "what share of this draft do I actually beat?" The direct lane target
        # uses the same role weight as MATCHUP so a +3 against a random support
        # cannot equal a +3 against the user's lane opponent.
        coverage_score = (
            100.0 * positive_weight / total_weight
            if total_weight > 0 else 0.0
        )

        return {
            "matchup_score": matchup_score,
            "coverage_score": coverage_score,
            "coverage_count": len(positives),
            "coverage_total": len(rows),
            "positive_strength": positive_strength,
            "negative_strength": negative_strength,
            "hard_counters": hard_counters,
            "avg_edge": avg_edge,
            "weighted_edge_sum": weighted_edge_sum,
            "positive_weight": positive_weight,
            "total_weight": total_weight,
            "positive": positives,
            "negative": negatives,
            "neutral": neutral,
            "direct_lane_edges": direct_lane_edges,
            "mirror_edge": mirror_edge,
            "enemy_roles": enemy_roles,
            "matrix_row": matrix_row,
        }

    def final_score(
        self,
        *,
        matchup_score: float,
        coverage_score: float,
        tier_score: float,
        winrate_score: float,
    ) -> dict:
        components = {
            "matchup": self.weights["matchup"] * float(matchup_score),
            "coverage": self.weights["coverage"] * float(coverage_score),
            "tier": self.weights["tier"] * float(tier_score),
            "winrate": self.weights["winrate"] * float(winrate_score),
        }
        components["score"] = sum(components.values())
        return components

    @staticmethod
    def feature_vector(threat_counts: Mapping[str, int | float]) -> dict[str, int]:
        """Stable build-side view of the current enemy draft.

        engine.py owns WRC text parsing and semantic trait discovery. This method
        only freezes the resulting counts into the feature names used by the
        adaptive builder and diagnostics.
        """
        aliases = {
            "tanks": "anti_tank",
            "duelists": "anti_duelist",
            "dive": "anti_dive",
            "burst": "anti_burst",
            "physical": "anti_physical",
            "magic": "anti_magic",
            "cc": "anti_cc",
            "engage": "anti_engage",
            "poke": "anti_poke",
            "healing": "anti_heal",
            "shields": "anti_shield",
            "mobility": "anti_mobility",
            "crit": "anti_crit",
            "attack_speed": "anti_attack_speed",
        }
        return {
            name: int(threat_counts.get(tag, 0) or 0)
            for name, tag in aliases.items()
        }
