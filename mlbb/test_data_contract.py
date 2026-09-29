from __future__ import annotations

import unittest

from collect_mlbb_snapshot import parse_counter_evidence, parse_lane_evidence
from data_contract import (
    LaneEvidence,
    MatchupEvidence,
    canonical_hero_id,
    canonical_lane,
    choose_supported_lanes,
    normalize_matchup_evidence,
)


class DataContractTests(unittest.TestCase):
    def test_names_and_source_ids_normalize_without_becoming_engine_keys(self):
        self.assertEqual(canonical_hero_id(17, "Fanny"), "17")
        self.assertEqual(canonical_hero_id("", "Yi Sun-shin"), "yisunshin")
        self.assertEqual(canonical_lane("EXP Lane"), "exp")
        self.assertEqual(canonical_lane("Roamer"), "roam")

    def test_assignment_evidence_does_not_invent_usage_rate(self):
        payload = {
            "data": {"records": [{"data": {
                "hero_id": 17,
                "hero": {"data": {"roadsort": [
                    {"data": {"road_sort_id": "4", "road_sort_title": "Jungle"}},
                    {"data": {"road_sort_id": "1", "road_sort_title": "EXP"}},
                ]}},
            }}]}
        }
        rows = parse_lane_evidence(payload, "17")
        self.assertEqual({row.lane for row in rows}, {"jungle", "exp"})
        self.assertTrue(all(row.usage_rate is None for row in rows))
        self.assertEqual(choose_supported_lanes(rows)["17"], ["exp", "jungle"])

    def test_usage_evidence_overrides_broad_assignment(self):
        rows = [
            LaneEvidence("1", "exp", "assignment", "catalog"),
            LaneEvidence("1", "mid", "assignment", "catalog"),
            LaneEvidence("1", "exp", "measured_usage", "stats", usage_rate=0.21),
            LaneEvidence("1", "mid", "measured_usage", "stats", usage_rate=0.02),
        ]
        self.assertEqual(choose_supported_lanes(rows, min_usage_rate=0.10)["1"], ["exp"])

    def test_raw_fraction_edges_are_preserved_and_normalized_separately(self):
        rows = [
            MatchupEvidence("a", "b", 0.01, "fraction", "test"),
            MatchupEvidence("a", "c", 0.04, "fraction", "test"),
            MatchupEvidence("a", "d", -0.02, "fraction", "test"),
        ]
        normalized, scale = normalize_matchup_evidence(rows, percentile=1.0)
        self.assertAlmostEqual(scale, 4.0)
        by_enemy = {row.enemy_id: row for row in normalized}
        self.assertAlmostEqual(by_enemy["b"].raw_edge, 0.01)
        self.assertAlmostEqual(by_enemy["b"].normalized_edge or 0.0, 0.25)
        self.assertAlmostEqual(by_enemy["c"].normalized_edge or 0.0, 1.0)
        self.assertAlmostEqual(by_enemy["d"].normalized_edge or 0.0, -0.5)

    def test_counter_parser_orients_candidate_to_target(self):
        payload = {"data": {"records": [{"data": {
            "main_heroid": 17,
            "sub_hero": [{"heroid": 1, "increase_win_rate": 0.025}],
            "sub_hero_last": [{"heroid": 2, "increase_win_rate": -0.015}],
        }}]}}
        rows = parse_counter_evidence(payload, "17", rank_segment="mythic")
        edges = {(row.champion_id, row.enemy_id): row.raw_edge for row in rows}
        self.assertAlmostEqual(edges[("1", "17")], 0.025)
        self.assertAlmostEqual(edges[("17", "1")], -0.025)
        self.assertAlmostEqual(edges[("2", "17")], -0.015)
        self.assertAlmostEqual(edges[("17", "2")], 0.015)


if __name__ == "__main__":
    unittest.main()
