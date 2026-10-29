import unittest
from unittest.mock import patch

import engine


class ThreatProfileCacheTests(unittest.TestCase):
    def test_reuses_profile_without_sharing_mutable_results(self):
        enemy = {"id": "Jax", "name": "Jax", "roles": ["Fighter"]}
        snapshot = {"champions_by_id": {"Jax": enemy}}
        with patch.object(engine, "_wrc_example_threat_tags", return_value={"anti_heal"}) as scan:
            first = engine._enemy_threat_profile([(enemy, "Барон")], snapshot)
            self.assertEqual(first[0]["anti_heal"], 1)
            self.assertEqual(first[1]["anti_heal"], ["Jax"])
            first[0]["anti_heal"] = 99
            first[1]["anti_heal"].append("Other")
            second = engine._enemy_threat_profile([(enemy, "Барон")], snapshot)
            self.assertEqual(second[0]["anti_heal"], 1)
            self.assertEqual(second[1]["anti_heal"], ["Jax"])
            self.assertEqual(scan.call_count, 1)

    def test_new_snapshot_recomputes_profile(self):
        enemy = {"id": "Jax", "name": "Jax", "roles": []}
        old = {"champions_by_id": {"Jax": enemy}}
        new = {"champions_by_id": {"Jax": enemy}}
        with patch.object(engine, "_wrc_example_threat_tags", side_effect=[{"anti_heal"}, {"anti_cc"}]):
            self.assertIn("anti_heal", engine._enemy_threat_profile([(enemy, "")], old)[0])
            self.assertIn("anti_cc", engine._enemy_threat_profile([(enemy, "")], new)[0])


if __name__ == "__main__":
    unittest.main()
