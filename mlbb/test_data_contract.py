from __future__ import annotations

import unittest

from collect_mlbb_snapshot import parse_counter_evidence, parse_lane_filter_evidence
from sources import parse_rone_academy_counter_raw
from moonton_gms import parse_gms_source
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
        jungle_payload = {
            "data": {"records": [{"data": {
                "hero_id": 17,
                "hero": {"data": {"name": "Fanny"}},
            }}]}
        }
        exp_payload = {
            "data": {"records": [{"data": {
                "hero_id": 17,
                "hero": {"data": {"name": "Fanny"}},
            }}]}
        }
        rows = [
            *parse_lane_filter_evidence(jungle_payload, "jungle"),
            *parse_lane_filter_evidence(exp_payload, "exp"),
        ]
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
            MatchupEvidence("a", "b", 0.01, "fraction", "test", evidence_type="measured:sub_hero"),
            MatchupEvidence("a", "c", 0.04, "fraction", "test", evidence_type="measured:sub_hero"),
            MatchupEvidence("a", "d", -0.02, "fraction", "test", evidence_type="measured:sub_hero_last"),
        ]
        normalized, scale = normalize_matchup_evidence(rows, percentile=1.0)
        self.assertAlmostEqual(scale, 4.0)
        by_enemy = {row.enemy_id: row for row in normalized}
        self.assertAlmostEqual(by_enemy["b"].raw_edge, 0.01)
        self.assertAlmostEqual(by_enemy["b"].normalized_edge or 0.0, 0.25)
        self.assertAlmostEqual(by_enemy["c"].normalized_edge or 0.0, 1.0)
        self.assertAlmostEqual(by_enemy["d"].normalized_edge or 0.0, -0.5)

    def test_direct_gms_keeps_raw_stats_and_matchup_swing(self):
        payload = {"data": {"records": [{"data": {
            "main_heroid": 1,
            "main_hero": {"data": {"name": "Miya", "head": "https://example/miya.png"}},
            "main_hero_win_rate": 0.5234,
            "main_hero_appearance_rate": 0.012,
            "main_hero_ban_rate": 0.034,
            "sub_hero": [{"heroid": 2, "increase_win_rate": 0.024}],
            "sub_hero_last": [{"heroid": 3, "increase_win_rate": 0.017}],
        }}]}}
        parsed = parse_gms_source(payload)
        self.assertEqual(parsed["heroes"][0]["id"], "1")
        self.assertAlmostEqual(parsed["stats"][0]["win_rate"], 52.34)
        edges = {(row.champion_id, row.enemy_id): row.raw_edge for row in parsed["matchups"]}
        self.assertAlmostEqual(edges[("1", "2")], 0.024)
        self.assertAlmostEqual(edges[("1", "3")], -0.017)

    def test_counter_parser_preserves_signed_candidate_to_target_edge(self):
        payload = {"data": {"records": [{"data": {
            "main_heroid": 17,
            "sub_hero": [
                {"heroid": 1, "increase_win_rate": 0.025},
                {"heroid": 2, "increase_win_rate": -0.011633},
            ],
            "sub_hero_last": [{"heroid": 3, "increase_win_rate": -0.015}],
        }}]}}
        rows = parse_counter_evidence(payload, "17", rank_segment="mythic")
        edges = {(row.champion_id, row.enemy_id): row.raw_edge for row in rows}
        self.assertAlmostEqual(edges[("1", "17")], 0.025)
        self.assertAlmostEqual(edges[("2", "17")], -0.011633)
        self.assertAlmostEqual(edges[("3", "17")], -0.015)
        self.assertNotIn(("17", "1"), edges)
        self.assertNotIn(("17", "2"), edges)
        self.assertNotIn(("17", "3"), edges)

    def test_runtime_academy_parser_does_not_abs_negative_subhero(self):
        payload = {"data": {"records": [{"data": {
            "main_heroid": 17,
            "sub_hero": [
                {"heroid": 39, "increase_win_rate": -0.011633},
                {"heroid": 20, "increase_win_rate": 0.048121},
            ],
        }}]}}
        rows = parse_rone_academy_counter_raw(payload, "17")
        edges = {(row["champion_id"], row["enemy_id"]): row["raw_edge"] for row in rows}
        self.assertAlmostEqual(edges[("39", "17")], -0.011633)
        self.assertAlmostEqual(edges[("20", "17")], 0.048121)
        self.assertNotIn(("17", "39"), edges)
        self.assertNotIn(("17", "20"), edges)


if __name__ == "__main__":
    unittest.main()
