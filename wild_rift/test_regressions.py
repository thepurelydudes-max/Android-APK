from __future__ import annotations

import os
import tempfile
import unittest

# Keep regression tests away from a developer's real runtime database.
_TEST_RUNTIME = tempfile.mkdtemp(prefix="wrca-test-")
os.environ["FLET_APP_STORAGE_DATA"] = _TEST_RUNTIME

import db
import engine
import sources
import updater
from paths import ensure_initial_data


def make_snapshot(
    champions,
    *,
    matchups=None,
    tiers=None,
    stats=None,
    items=None,
    item_pools=None,
    counter_items=None,
    role_builds=None,
    role_situational=None,
    role_boots=None,
):
    by_id = {c["id"]: dict(c) for c in champions}
    aliases = {}
    alias_ids = {}
    for champ in by_id.values():
        for value in (champ.get("id", ""), champ.get("name", ""), champ.get("name_ru", "")):
            key = db.normalize_search(value or "")
            if key:
                aliases[key] = champ
                alias_ids.setdefault(key, set()).add(champ["id"])
    return {
        "champions": list(by_id.values()),
        "champions_by_id": by_id,
        "champion_aliases": aliases,
        "champion_alias_ids": alias_ids,
        "matchups": matchups or {},
        "tiers": tiers or {},
        "stats": stats or {},
        "items": items or {},
        "item_pools": item_pools or {},
        "counter_items": counter_items or {},
        "role_builds": role_builds or {},
        "role_situational": role_situational or {},
        "role_boots": role_boots or {},
    }


class RecommendationRegressionTests(unittest.TestCase):
    def test_visible_total_score_is_the_primary_pick_order(self):
        champions = [
            {"id": "Malphite", "name": "Malphite", "name_ru": "Мальфит", "roles": ["Tank"], "lanes": ["top"], "damage_type": "Magic"},
            {"id": "Jax", "name": "Jax", "name_ru": "Джакс", "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical"},
            {"id": "Irelia", "name": "Irelia", "name_ru": "Ирелия", "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical"},
        ]
        snapshot = make_snapshot(
            champions,
            matchups={
                ("Malphite", "Irelia"): [("Барон", 1.0)],
                ("Jax", "Irelia"): [("Барон", 2.0)],
            },
            tiers={
                ("Malphite", "Барон"): "S+",
                ("Jax", "Барон"): "D",
            },
            stats={
                ("Malphite", "top", "all"): {"win_rate": 60.0, "pick_rate": 5.0},
                ("Jax", "top", "all"): {"win_rate": 48.0, "pick_rate": 5.0},
            },
        )

        results = engine.recommend_picks(
            "Барон", [("Irelia", "Барон")], limit=8, snapshot=snapshot
        )
        self.assertEqual([row["champion"]["id"] for row in results], ["Malphite", "Jax"])
        self.assertGreater(results[0]["score"], results[1]["score"])
        self.assertEqual(
            [row["score"] for row in results],
            sorted((row["score"] for row in results), reverse=True),
        )

    def test_missing_role_build_falls_back_to_existing_champion_item_pool(self):
        champions = [
            {"id": "Malphite", "name": "Malphite", "name_ru": "Мальфит", "roles": ["Tank"], "lanes": ["top"], "damage_type": "Magic"},
            {"id": "Irelia", "name": "Irelia", "name_ru": "Ирелия", "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical"},
        ]
        names = [
            "Randuin's Omen",
            "Thornmail",
            "Sunfire Aegis",
            "Radiant Virtue",
            "Amaranth's Twinguard",
            "Plated Steelcaps",
        ]
        items = {
            name: {
                "name": name,
                "category": "Boots" if name == "Plated Steelcaps" else "Defense",
                "tier": "Upgraded",
                "stats_json": "[]",
                "effect_en": "",
            }
            for name in names
        }
        item_pools = {
            "Malphite": [
                {
                    "champion_id": "Malphite",
                    "item_name": name,
                    "category": "Boots" if name == "Plated Steelcaps" else "Defense",
                    "priority": index + 1,
                    "source": "wrpocket.app",
                }
                for index, name in enumerate(names)
            ]
        }
        snapshot = make_snapshot(
            champions,
            matchups={("Malphite", "Irelia"): [("Барон", 1.0)]},
            items=items,
            item_pools=item_pools,
        )

        build = engine.recommend_build(
            "Malphite", [("Irelia", "Барон")], role_ru="Барон", snapshot=snapshot
        )
        self.assertTrue(build.get("fallback_used"))
        self.assertEqual(build.get("source"), "wrpocket.app:fallback")
        self.assertGreaterEqual(len(build.get("ordered") or []), 5)
        self.assertIn("Plated Steelcaps", build.get("ordered") or [])

    def test_healthy_role_build_remains_preferred_over_fallback(self):
        champions = [
            {"id": "Malphite", "name": "Malphite", "name_ru": "Мальфит", "roles": ["Tank"], "lanes": ["top"], "damage_type": "Magic"},
            {"id": "Irelia", "name": "Irelia", "name_ru": "Ирелия", "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical"},
        ]
        core = ["Randuin's Omen", "Thornmail", "Sunfire Aegis", "Radiant Virtue", "Amaranth's Twinguard"]
        boot = "Plated Steelcaps"
        items = {
            name: {
                "name": name,
                "category": "Boots" if name == boot else "Defense",
                "tier": "Upgraded",
                "stats_json": "[]",
                "effect_en": "",
            }
            for name in core + [boot]
        }
        snapshot = make_snapshot(
            champions,
            items=items,
            role_builds={
                ("Malphite", "Барон"): {
                    "champion_id": "Malphite",
                    "role": "Барон",
                    "items": core,
                    "boot_name": boot,
                    "source": "wildriftcore.com",
                    "source_url": "https://example.invalid/malphite/builds",
                }
            },
        )

        build = engine.recommend_build(
            "Malphite", [("Irelia", "Барон")], role_ru="Барон", snapshot=snapshot
        )
        self.assertFalse(build.get("fallback_used", False))
        self.assertEqual(build.get("source"), "wildriftcore.com")
        self.assertEqual(build.get("ordered"), core + [boot])

    def test_wrpocket_profile_identity_does_not_collapse_to_zyra(self):
        index_html = """
        <html><body>
          <a href="/en/champions/aatrox">Zyra S+ 99 Aatrox decorated card</a>
          <a href="/en/champions/ahri">Zyra S+ 99 Ahri decorated card</a>
          <a href="/en/champions/zyra">Zyra S+ 99 Zyra decorated card</a>
        </body></html>
        """
        page = """
        <html><body>
          <h2>Items</h2><h4>Defense</h4>
          <a href="/en/items/thornmail">Thornmail</a>
          <h2>Runes</h2>
        </body></html>
        """

        class Response:
            def __init__(self, text):
                self.text = text

        class FakeNet:
            def get(self, url, *args, **kwargs):
                if url.rstrip("/") == sources.WR_POCKET_CHAMPS.rstrip("/"):
                    return Response(index_html)
                return Response(page)

        mapping = {"aatrox": "Aatrox", "ahri": "Ahri", "zyra": "Zyra"}
        progress = []
        rows = sources.fetch_wrpocket_item_pools(
            FakeNet(),
            lambda value: mapping.get(sources.slugish(value)),
            progress.append,
        )
        self.assertEqual({row[0] for row in rows}, {"Aatrox", "Ahri", "Zyra"})
        joined = "\n".join(progress)
        self.assertIn("— Aatrox", joined)
        self.assertIn("— Ahri", joined)
        self.assertIn("— Zyra", joined)


class SourceIntegrityRegressionTests(unittest.TestCase):
    def test_tier_integrity_detects_a_resolved_champion_without_tier_row(self):
        names = [f"Hero{i}" for i in range(12)]
        links = "".join(
            f'<a href="/en/champions/{name.casefold()}">{name}</a>'
            for name in names
        )
        # Hero11 is deliberately placed after the last tier heading with no tier
        # assignment in a structure the tier parser cannot classify, while the
        # integrity scanner can still see its champion profile link.
        html = (
            '<h2>Every Top champion ranked, S+ to C</h2>'
            '<div><a href="/en/champions/hero11">Hero11</a></div>'
            '<h3>S+</h3>'
            + "".join(
                f'<a href="/en/champions/{name.casefold()}">S+ {name}</a>'
                for name in names[:11]
            )
            + '<h2>How do we calculate this tier list?</h2>'
        )
        mapping = {name.casefold(): name for name in names}
        resolve = lambda value: mapping.get(sources.slugish(value))
        parsed = sources._parse_wildriftcore_tier_page(html, "Барон", resolve)
        expected_ids, unresolved = sources._wildriftcore_ranked_section_ids(html, resolve)
        parsed_ids = {row[0] for row in parsed}
        self.assertFalse(unresolved)
        self.assertIn("Hero11", expected_ids)
        self.assertIn("Hero11", expected_ids - parsed_ids)

    def test_exact_item_detail_icon_replaces_catalog_card_icon(self):
        record = {
            "name": "Sunfire Aegis",
            "tier": "Upgraded",
            "detail_url": "https://wrpocket.app/en/items/sunfire-aegis",
            "icon_url": "https://example.invalid/wrong.png",
            "price": 2900,
            "stats": [],
            "effect_en": "",
        }
        html = (
            '<html><body>'
            '<img src="https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/lol_rydp.png" alt="サンファイアイージス">'
            '<h1>Sunfire Aegis</h1>'
            '<h2>Recipe</h2>'
            '<img src="https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/component.png">'
            '</body></html>'
        )

        class Response:
            text = html

        class FakeNet:
            def get(self, _url, *args, **kwargs):
                return Response()

        rows = sources.verify_wrpocket_item_icons(FakeNet(), [record])
        self.assertTrue(rows[0].get("_icon_verified"))
        self.assertEqual(
            rows[0]["icon_url"],
            "https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/lol_rydp.png",
        )

    def test_unverified_catalog_icon_is_not_allowed_to_overwrite_database(self):
        record = {
            "name": "Sunfire Aegis",
            "tier": "Upgraded",
            "detail_url": "https://wrpocket.app/en/items/sunfire-aegis",
            "icon_url": "https://example.invalid/wrong.png",
            "price": 2900,
            "stats": [],
            "effect_en": "",
        }

        class Response:
            text = '<html><body><h1>Sunfire Aegis</h1><h2>Recipe</h2></body></html>'

        class FakeNet:
            def get(self, _url, *args, **kwargs):
                return Response()

        rows = sources.verify_wrpocket_item_icons(FakeNet(), [record])
        self.assertFalse(rows[0].get("_icon_verified"))
        self.assertEqual(rows[0].get("icon_url"), "")

    def test_cleared_icon_path_does_not_revalidate_leftover_png(self):
        db.init_db()
        with tempfile.TemporaryDirectory(prefix="wrca-icon-") as folder:
            target = os.path.join(folder, "item.png")
            with open(target, "wb") as handle:
                handle.write(b"old-but-untrusted")
            seeded = updater._seed_media_record(
                "item:RegressionOnly",
                "https://game.gtimg.cn/new-icon.png",
                {"icon_url": "https://game.gtimg.cn/new-icon.png", "icon_path": ""},
                __import__("pathlib").Path(target),
                "7.3",
                "7.3",
            )
            self.assertIsNone(seeded)


class BundledDatabaseSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_initial_data()
        db.init_db()
        cls.snapshot = db.load_runtime_snapshot()

    def test_real_database_pick_list_is_monotonic_by_visible_score(self):
        enemies = [
            ("Irelia", "Барон"),
            ("Hecarim", "Лес"),
            ("Vex", "Мид"),
            ("Kaisa", "ADC"),
            ("Thresh", "Саппорт"),
        ]
        available = [
            (name, role)
            for name, role in enemies
            if db.resolve_snapshot_champion(self.snapshot, name)
        ]
        self.assertGreaterEqual(len(available), 4)
        results = engine.recommend_picks("Барон", available, limit=8, snapshot=self.snapshot)
        self.assertGreaterEqual(len(results), 3)
        scores = [float(row.get("score") or 0.0) for row in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_real_database_top_picks_do_not_render_six_empty_item_slots(self):
        enemies = [
            ("Irelia", "Барон"),
            ("Hecarim", "Лес"),
            ("Vex", "Мид"),
            ("Kaisa", "ADC"),
            ("Thresh", "Саппорт"),
        ]
        available = [
            (name, role)
            for name, role in enemies
            if db.resolve_snapshot_champion(self.snapshot, name)
        ]
        results = engine.recommend_picks("Барон", available, limit=5, snapshot=self.snapshot)
        self.assertGreaterEqual(len(results), 3)
        empty = []
        for result in results:
            champ = result["champion"]
            build = engine.recommend_build(
                champ.get("id") or champ.get("name"),
                available,
                role_ru="Барон",
                snapshot=self.snapshot,
            )
            if len(build.get("ordered") or []) < 3:
                empty.append(champ.get("id") or champ.get("name"))
        self.assertFalse(empty, "Empty/incomplete item previews: " + ", ".join(empty))


if __name__ == "__main__":
    unittest.main(verbosity=2)
