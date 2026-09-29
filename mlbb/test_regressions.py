from __future__ import annotations

import unittest

import engine
from draft_matrix_engine import DraftEdge, DraftMatrixEngine


class DraftMatrixTests(unittest.TestCase):
    def test_weights_and_lane_priority_match_wrc_model(self):
        matrix = DraftMatrixEngine()
        self.assertAlmostEqual(sum(matrix.weights.values()), 1.0)
        self.assertEqual(matrix.lane_weight("EXP", "EXP"), 2.0)
        self.assertEqual(matrix.lane_weight("Мид", "Мид"), 2.0)
        self.assertEqual(matrix.lane_weight("Лес", "Лес"), 1.5)
        self.assertEqual(matrix.lane_weight("Голд", "Роум"), 1.5)
        self.assertEqual(matrix.lane_weight("EXP", "Роум"), 1.0)

    def test_matchup_scale_is_mlbb_1_5(self):
        matrix = DraftMatrixEngine()
        row = matrix.analyze_row([
            DraftEdge("enemy", "Enemy", "EXP", 1.5, 2.0),
        ])
        self.assertAlmostEqual(row["matchup_score"], 100.0)
        row = matrix.analyze_row([
            DraftEdge("enemy", "Enemy", "EXP", -1.5, 2.0),
        ])
        self.assertAlmostEqual(row["matchup_score"], 0.0)

    def test_counter_labels_require_real_threshold_but_scoring_stays_continuous(self):
        matrix = DraftMatrixEngine()
        row = matrix.analyze_row([
            DraftEdge("small_plus", "Small Plus", "EXP", 0.4, 1.0),
            DraftEdge("real_counter", "Real Counter", "MID", 1.3, 1.0),
            DraftEdge("small_minus", "Small Minus", "JUNGLE", -0.3, 1.0),
            DraftEdge("real_threat", "Real Threat", "ROAM", -1.4, 1.0),
        ], counter_threshold=1.2)
        self.assertEqual(row["positive"], ["Real Counter"])
        self.assertEqual(row["negative"], ["Real Threat"])
        self.assertEqual(set(row["neutral"]), {"Small Plus", "Small Minus"})
        self.assertEqual(row["coverage_count"], 1)
        # The small +0.4 still contributes to positive coverage/scoring even
        # though it is no longer presented as a real counter to the user.
        self.assertAlmostEqual(row["positive_weight"], 2.0)
        self.assertGreater(row["matchup_score"], 50.0 - 20.0)


class EngineTests(unittest.TestCase):
    def test_relative_counter_group_finds_dominant_three(self):
        edges = [
            DraftEdge("e1", "E1", "", 0.30, 1.0),
            DraftEdge("e2", "E2", "", 0.27, 1.0),
            DraftEdge("e3", "E3", "", 0.23, 1.0),
            DraftEdge("e4", "E4", "", 0.12, 1.0),
            DraftEdge("e5", "E5", "", 0.08, 1.0),
        ]
        names, shares = engine._dominant_matchup_targets(edges, positive=True)
        self.assertEqual(names, ["E1", "E2", "E3"])
        self.assertAlmostEqual(sum(shares.values()), 100.0)

    def test_relative_counter_group_keeps_even_five(self):
        edges = [
            DraftEdge(str(i), f"E{i}", "", 0.20, 1.0)
            for i in range(1, 6)
        ]
        names, _shares = engine._dominant_matchup_targets(edges, positive=True)
        self.assertEqual(names, ["E1", "E2", "E3", "E4", "E5"])

    def test_relative_counter_group_keeps_only_clear_dominator(self):
        edges = [
            DraftEdge("e1", "E1", "", 0.70, 1.0),
            DraftEdge("e2", "E2", "", 0.10, 1.0),
            DraftEdge("e3", "E3", "", 0.08, 1.0),
            DraftEdge("e4", "E4", "", 0.07, 1.0),
            DraftEdge("e5", "E5", "", 0.05, 1.0),
        ]
        names, _shares = engine._dominant_matchup_targets(edges, positive=True)
        self.assertEqual(names, ["E1"])

    def test_pick_ranking_uses_full_draft_matrix(self):
        snapshot = {
            "champions": [
                {"id": "a", "name": "A", "name_ru": "А", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
                {"id": "b", "name": "B", "name_ru": "Б", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
                {"id": "e1", "name": "Enemy1", "name_ru": "Враг1", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
                {"id": "e2", "name": "Enemy2", "name_ru": "Враг2", "lanes": ["mid"], "roles": ["mage"], "damage_type": "magic"},
            ],
            "champions_by_id": {},
            "champion_aliases": {},
            "champion_alias_ids": {},
            "matchups": {
                ("a", "e1"): [("", 1.2)],
                ("a", "e2"): [("", 0.8)],
                ("b", "e1"): [("", -1.2)],
                ("b", "e2"): [("", 1.5)],
            },
            "tiers": {("a", "EXP"): "A", ("b", "EXP"): "S+"},
            "stats": {
                ("a", "exp", "all"): {"win_rate": 51.0},
                ("b", "exp", "all"): {"win_rate": 55.0},
            },
            "item_pools": {},
            "counter_items": {},
            "role_builds": {
                ("a", "EXP"): {"items": ["Blade Armor", "Corrosion Scythe", "Demon Hunter Sword"], "boot_name": "", "source": "test"},
                ("b", "EXP"): {"items": ["Blade Armor", "Corrosion Scythe", "Demon Hunter Sword"], "boot_name": "", "source": "test"},
            },
            "role_variants": {},
            "role_situational": {},
            "role_boots": {},
            "role_opponent_adaptations": {},
            "items": {
                name: {"name": name, "tier": "Upgraded", "category": "Attack"}
                for name in ["Blade Armor", "Corrosion Scythe", "Demon Hunter Sword"]
            },
        }
        snapshot["champions_by_id"] = {c["id"]: c for c in snapshot["champions"]}
        snapshot["champion_aliases"] = {c["name"].casefold(): c for c in snapshot["champions"]}
        snapshot["champion_alias_ids"] = {c["name"].casefold(): {c["id"]} for c in snapshot["champions"]}
        result = engine.recommend_picks("EXP", [("Enemy1", ""), ("Enemy2", "")], snapshot=snapshot)
        self.assertEqual(result[0]["champion"]["id"], "a")
        self.assertTrue(result[0]["matrix_row"])

    def test_pick_ranking_uses_raw_five_enemy_sum(self):
        champions = [
            {"id": "a", "name": "A", "name_ru": "А", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
            {"id": "b", "name": "B", "name_ru": "Б", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
            {"id": "e1", "name": "E1", "name_ru": "E1", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
            {"id": "e2", "name": "E2", "name_ru": "E2", "lanes": ["mid"], "roles": ["mage"], "damage_type": "magic"},
        ]
        items = {
            name: {"name": name, "tier": "Upgraded", "category": "Attack"}
            for name in ["Axe", "Spear", "Bow"]
        }
        snapshot = {
            "champions": champions,
            "champions_by_id": {row["id"]: row for row in champions},
            "champion_aliases": {row["name"].casefold(): row for row in champions},
            "champion_alias_ids": {row["name"].casefold(): {row["id"]} for row in champions},
            "matchups": {
                ("a", "e1"): [("", 1.5)], ("a", "e2"): [("", 1.5)],
                ("b", "e1"): [("", 0.2)], ("b", "e2"): [("", 0.2)],
            },
            "matchup_raw_pp": {
                ("a", "e1"): [("", 1.0)], ("a", "e2"): [("", 1.0)],
                ("b", "e1"): [("", 3.0)], ("b", "e2"): [("", -0.5)],
            },
            "tiers": {("a", "EXP"): "S+", ("b", "EXP"): "A"},
            "stats": {("a", "exp", "all"): {"win_rate": 60.0}, ("b", "exp", "all"): {"win_rate": 50.0}},
            "item_pools": {}, "counter_items": {},
            "role_builds": {
                ("a", "EXP"): {"items": ["Axe", "Spear", "Bow"], "boot_name": "", "source": "test"},
                ("b", "EXP"): {"items": ["Axe", "Spear", "Bow"], "boot_name": "", "source": "test"},
            },
            "role_variants": {}, "role_situational": {}, "role_boots": {},
            "role_opponent_adaptations": {}, "items": items,
        }
        result = engine.recommend_picks("EXP", [("E1", ""), ("E2", "")], snapshot=snapshot)
        self.assertEqual(result[0]["champion"]["id"], "b")
        self.assertAlmostEqual(result[0]["draft_matchup_sum_pp"], 2.5)
        self.assertEqual(result[0]["positive"], ["E1"])
    def test_pick_excludes_hero_without_selected_role_build(self):
        items = {
            name: {"name": name, "tier": "Upgraded", "category": "Attack"}
            for name in ["Axe", "Spear", "Bow"]
        }
        champions = [
            {"id": "a", "name": "A", "name_ru": "А", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
            {"id": "b", "name": "B", "name_ru": "Б", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
            {"id": "e", "name": "Enemy", "name_ru": "Враг", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
        ]
        snapshot = {
            "champions": champions,
            "champions_by_id": {c["id"]: c for c in champions},
            "champion_aliases": {c["name"].casefold(): c for c in champions},
            "champion_alias_ids": {c["name"].casefold(): {c["id"]} for c in champions},
            "matchups": {("a", "e"): [("", 0.2)], ("b", "e"): [("", 1.5)]},
            "tiers": {("a", "EXP"): "A", ("b", "EXP"): "S+"},
            "stats": {("a", "exp", "all"): {"win_rate": 50.0}, ("b", "exp", "all"): {"win_rate": 60.0}},
            "item_pools": {}, "counter_items": {},
            "role_builds": {("a", "EXP"): {"items": ["Axe", "Spear", "Bow"], "boot_name": "", "source": "test"}},
            "role_variants": {}, "role_situational": {}, "role_boots": {},
            "role_opponent_adaptations": {}, "items": items,
        }
        result = engine.recommend_picks("EXP", [("Enemy", "")], snapshot=snapshot)
        self.assertEqual([row["champion"]["id"] for row in result], ["a"])

    def test_pick_accepts_two_core_plus_boots_measured_footprint(self):
        items = {
            "Axe": {"name": "Axe", "tier": "Upgraded", "category": "Attack"},
            "Spear": {"name": "Spear", "tier": "Upgraded", "category": "Attack"},
            "Tough Boots": {"name": "Tough Boots", "tier": "Upgraded", "category": "Movement"},
        }
        champions = [
            {"id": "a", "name": "A", "name_ru": "А", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
            {"id": "e", "name": "Enemy", "name_ru": "Враг", "lanes": ["exp"], "roles": ["fighter"], "damage_type": "physical"},
        ]
        snapshot = {
            "champions": champions,
            "champions_by_id": {c["id"]: c for c in champions},
            "champion_aliases": {c["name"].casefold(): c for c in champions},
            "champion_alias_ids": {c["name"].casefold(): {c["id"]} for c in champions},
            "matchups": {("a", "e"): [("", 0.4)]},
            "tiers": {("a", "EXP"): "A"},
            "stats": {("a", "exp", "all"): {"win_rate": 51.0}},
            "item_pools": {}, "counter_items": {},
            "role_builds": {("a", "EXP"): {"items": ["Axe", "Spear"], "boot_name": "Tough Boots", "source": "test"}},
            "role_variants": {}, "role_situational": {}, "role_boots": {},
            "role_opponent_adaptations": {}, "items": items,
        }
        result = engine.recommend_picks("EXP", [("Enemy", "")], snapshot=snapshot)
        self.assertEqual([row["champion"]["id"] for row in result], ["a"])

    def test_role_build_keeps_core_and_adapts_flexible_slot(self):
        champion = {"id": "a", "name": "A", "name_ru": "А", "lanes": ["gold"], "roles": ["marksman"], "damage_type": "physical"}
        enemy = {"id": "e", "name": "Enemy", "name_ru": "Враг", "lanes": ["gold"], "roles": ["marksman"], "damage_type": "physical"}
        names = ["Corrosion Scythe", "Demon Hunter Sword", "Golden Staff", "Malefic Roar", "Windtalker", "Swift Boots", "Blade Armor"]
        items = {name: {"name": name, "tier": "Upgraded", "category": "Movement" if "Boots" in name else "Attack"} for name in names}
        snapshot = {
            "champions": [champion, enemy],
            "champions_by_id": {"a": champion, "e": enemy},
            "champion_aliases": {"a": champion, "enemy": enemy},
            "champion_alias_ids": {"a": {"a"}, "enemy": {"e"}},
            "matchups": {("a", "e"): [("", -1.0)]},
            "tiers": {},
            "stats": {},
            "item_pools": {
                "a": [
                    {"item_name": name, "category": items[name]["category"], "priority": i + 1, "source": "test"}
                    for i, name in enumerate(names)
                ]
            },
            "counter_items": {
                "e": [{"enemy_id": "e", "item_name": "Blade Armor", "reason": "anti basic", "source": "test"}]
            },
            "role_builds": {
                ("a", "Голд"): {
                    "items": ["Corrosion Scythe", "Demon Hunter Sword", "Golden Staff", "Malefic Roar", "Windtalker"],
                    "boot_name": "Swift Boots",
                    "source": "mlbb.rone",
                }
            },
            "role_variants": {},
            "role_situational": {
                ("a", "Голд"): [{"item_name": "Blade Armor", "priority": 1, "source": "mlbb.rone"}]
            },
            "role_boots": {
                ("a", "Голд"): [{"item_name": "Swift Boots", "priority": 1, "source": "mlbb.rone"}]
            },
            "role_opponent_adaptations": {},
            "items": items,
        }
        build = engine.recommend_build("A", [("Enemy", "")], role_ru="Голд", snapshot=snapshot)
        self.assertEqual(build["ordered"][:3], ["Corrosion Scythe", "Demon Hunter Sword", "Golden Staff"])
        self.assertIn("Blade Armor", build["ordered"])
        self.assertEqual(sum(1 for x in build["ordered"] if engine.is_boot_item(x)), 1)


if __name__ == "__main__":
    unittest.main()
