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
    def test_trusted_icon_urls_are_name_addressed_for_reported_bad_items(self):
        expected = {
            "Kaenic Rookern": "https://www.wildriftmeta.com/assets/item/icon/item-kaenic-rookern-icon.png",
            "Sundered Sky": "https://www.wildriftmeta.com/assets/item/icon/item-sundered-sky-icon.png",
            "Mercury's Treads": "https://wildriftcore.com/assets/images/newItems/mercurys_treads.webp",
        }
        for name, url in expected.items():
            urls = sources.trusted_item_icon_urls(name)
            self.assertGreaterEqual(len(urls), 2)
            self.assertEqual(urls[0], url)
            self.assertNotIn("wrpocket.app", " ".join(urls))

    def test_mercury_boots_shorthand_maps_to_real_patch_item(self):
        self.assertEqual(
            sources.canonical_item_name("Mercury Boots"),
            "Mercury's Treads",
        )
        self.assertEqual(
            sources.canonical_item_name("Mercury Treads"),
            "Mercury's Treads",
        )

    def test_reported_patch_73_icon_failures_have_third_wr_native_candidate(self):
        names = [
            "Fiendhunter Bolts",
            "Rapid Firecannon",
            "Whispering Circlet",
            "Yun Tal Wildarrows",
        ]
        for name in names:
            urls = sources.trusted_item_icon_urls(name)
            self.assertGreaterEqual(len(urls), 3)
            self.assertIn(
                "https://wildriftcore.com/assets/images/items-cn/",
                urls[2],
            )
            self.assertTrue(urls[2].endswith(".webp"))

    def test_verified_icon_fallback_reads_exact_wrc_item_image(self):
        html = (
            '<html><body>'
            '<img src="/assets/images/items-cn/fiendhunter-bolts.webp" '
            'alt="Fiendhunter Bolts">'
            '<h1>Fiendhunter Bolts</h1>'
            '<p>Cost: 2650 Category: Damage Patch: 7.3</p>'
            '</body></html>'
        )

        class Response:
            status_code = 200
            headers = {}
            text = html
            def raise_for_status(self):
                return None

        class FakeNet:
            timeout = 1
            _wildriftcore_gap = 0.0
            _wildriftcore_last_request = 0.0
            def __init__(self):
                self.s = self
            def get(self, url, *args, **kwargs):
                if "wildriftcore.com" in url:
                    return Response()
                return type("MetaResponse", (), {
                    "text": "<html><h1>Fiendhunter Bolts</h1></html>"
                })()

        urls = sources.fetch_verified_item_icon_urls(
            FakeNet(), "Fiendhunter Bolts"
        )
        self.assertIn(
            "https://wildriftcore.com/assets/images/items-cn/fiendhunter-bolts.webp",
            urls,
        )

    def test_placeholder_svg_is_not_accepted_as_item_art(self):
        self.assertFalse(
            sources._usable_item_icon_url(
                "https://wildriftcore.com/assets/images/placeholders/item.svg"
            )
        )

    def test_mercury_treads_uses_real_wrc_boot_asset(self):
        urls = sources.trusted_item_icon_urls("Mercury Boots")
        self.assertEqual(
            urls[0],
            "https://wildriftcore.com/assets/images/newItems/mercurys_treads.webp",
        )
        self.assertEqual(
            sources.canonical_item_name("Mercury Boots"),
            "Mercury's Treads",
        )

    def test_item_icon_source_trust_accepts_real_wr_assets_not_wrpocket_cards(self):
        self.assertTrue(sources.is_trusted_item_icon_url(
            "https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/lol_bxzx.png"
        ))
        self.assertTrue(sources.is_trusted_item_icon_url(
            "https://wildriftcore.com/assets/images/items-cn/rapid-firecannon.webp"
        ))
        self.assertTrue(sources.is_trusted_item_icon_url(
            "https://wildriftcore.com/assets/images/newItems/mercurys_treads.webp"
        ))
        self.assertFalse(sources.is_trusted_item_icon_url(
            "https://wrpocket.app/assets/img/items/lol_fjdp.webp"
        ))
        self.assertFalse(sources.is_trusted_item_icon_url(
            "https://wildriftcore.com/assets/images/placeholders/item.svg"
        ))

    def test_current_patch_boot_names_keep_their_identity(self):
        self.assertEqual(
            sources.canonical_completed_item_name("Mercury's Treads"),
            "Mercury's Treads",
        )
        self.assertEqual(
            sources.canonical_completed_item_name("Plated Steelcaps"),
            "Plated Steelcaps",
        )
        self.assertEqual(
            sources.canonical_completed_item_name("Boots of Mana"),
            "Boots of Mana",
        )

    def test_possessive_item_asset_slug_does_not_insert_fake_hyphen(self):
        self.assertIn(
            "https://www.wildriftmeta.com/assets/item/icon/item-mercurys-treads-icon.png",
            sources.trusted_item_icon_urls("Mercury's Treads"),
        )
        self.assertEqual(
            sources.trusted_item_icon_urls("Randuin's Omen")[0],
            "https://www.wildriftmeta.com/assets/item/icon/item-randuins-omen-icon.png",
        )

    def test_live_roster_supplement_can_add_norra_identity(self):
        html = (
            '<html><body>'
            '<a href="/champions/ahri/">Ahri</a>'
            '<a href="/champions/norra/">Norra</a>'
            '<a href="/champions/zyra/">Zyra</a>'
            '</body></html>'
        )

        class Response:
            text = html

        class FakeNet:
            def get(self, _url, *args, **kwargs):
                return Response()

        rows = sources.fetch_wildriftmeta_champion_roster(FakeNet())
        by_id = {row["id"]: row for row in rows}
        self.assertIn("Norra", by_id)
        self.assertEqual(by_id["Norra"]["name"], "Norra")
        self.assertTrue(by_id["Norra"]["icon_url"].endswith("champion-norra-icon.png"))

    def test_profile_url_uses_exact_id_not_contaminated_display_name(self):
        # Reproduce the real warning: a future/unknown /norra profile must never
        # collapse into Zyra even if some upstream display-name field is wrong.
        champions = [{
            "id": "Zyra", "name": "Zyra", "name_ru": "Norra",
            "roles": ["mage", "support"], "lanes": ["mid", "support"],
            "damage_type": "Mana",
        }]
        champions.extend({
            "id": f"Hero{i}", "name": f"Hero{i}", "name_ru": "",
            "roles": ["mage"], "lanes": ["mid"], "damage_type": "Mana",
        } for i in range(19))
        resolver = updater.build_resolver(champions)
        self.assertEqual(resolver("Norra"), "Zyra")  # loose text resolver may see the bad label
        self.assertIsNone(resolver.exact_id("norra"))
        self.assertEqual(resolver.exact_id("zyra"), "Zyra")

        index_html = (
            '<a href="/en/champions/norra">Norra</a>'
            '<a href="/en/champions/zyra">Zyra</a>'
            + "".join(
                f'<a href="/en/champions/hero{i}">Hero{i}</a>'
                for i in range(19)
            )
        )
        page = (
            '<h2>Items</h2><h4>Magic</h4>'
            '<a href="/en/items/morellonomicon">Morellonomicon</a>'
            '<h2>Runes</h2>'
        )

        class Response:
            def __init__(self, text):
                self.text = text

        class FakeNet:
            def get(self, url, *args, **kwargs):
                if url.rstrip("/") == sources.WR_POCKET_CHAMPS.rstrip("/"):
                    return Response(index_html)
                return Response(page)

        progress = []
        rows = sources.fetch_wrpocket_item_pools(FakeNet(), resolver, progress.append)
        self.assertEqual(
            {row[0] for row in rows},
            {"Zyra", *{f"Hero{i}" for i in range(19)}},
        )
        self.assertIn("неизвестный профиль norra", "\n".join(progress))

    def test_wildriftcounter_retries_remote_disconnect(self):
        original_sleep = sources.time.sleep
        sources.time.sleep = lambda _seconds: None
        try:
            class Response:
                status_code = 200
                headers = {}
                text = "<html></html>"
                def raise_for_status(self):
                    return None

            class Session:
                def __init__(self):
                    self.calls = 0
                def get(self, *_args, **_kwargs):
                    self.calls += 1
                    if self.calls == 1:
                        raise sources.requests.ConnectionError("remote closed")
                    return Response()

            class FakeNet:
                timeout = 1
                _wildriftcounter_last_request = 0.0
                _wildriftcounter_gap = 0.0
                s = Session()

            net = FakeNet()
            response = sources._wildriftcounter_get(net, "https://wildriftcounter.com/champions/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(net.s.calls, 2)
        finally:
            sources.time.sleep = original_sleep

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

    def test_trusted_cached_item_survives_failed_revalidation(self):
        from pathlib import Path
        from PIL import Image
        from media_cache import ITEM_DIR, safe_name

        db.init_db()
        name = "Regression Trusted Cached Item"
        icon_url = (
            "https://game.gtimg.cn/images/lgamem/act/lrlib/img/EquipIcons/"
            "regression_trusted.png"
        )
        target = ITEM_DIR / f"{safe_name(name)}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGBA", (8, 8), (255, 255, 255, 255)).save(target, "PNG")
        portable = updater._portable_path(str(target))

        db.upsert_item(
            name, "Defense", "regression", icon_url=icon_url,
            icon_path=portable, tier="Upgraded",
        )
        db.delete_media_asset(f"item:{name}")

        class FailingNet:
            _item_icon_last_request = 0.0
            def get(self, *_args, **_kwargs):
                raise RuntimeError("temporary network failure")

        errors = []
        try:
            _champions, item_count, item_failures = updater._cache_media(
                FailingNet(), [], [(name, "Defense", icon_url)], "",
                lambda _message: None,
                current_patch="7.3", previous_patch="7.3",
                errors=errors, force_item_refresh=False,
            )
            row = db.get_item(name) or {}
            self.assertEqual(item_failures, 0)
            self.assertEqual(item_count, 1)
            self.assertEqual(row.get("icon_path"), portable)
            self.assertFalse(any("Item media cache" in value for value in errors))
        finally:
            target.unlink(missing_ok=True)
            with db.connect() as con:
                con.execute("DELETE FROM media_assets WHERE asset_key=?", (f"item:{name}",))
                con.execute("DELETE FROM items WHERE name=?", (name,))

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


class PerformanceRegressionTests(unittest.TestCase):
    def test_enemy_role_inference_is_cached_for_same_snapshot_and_draft(self):
        champions = [
            {"id": "A", "name": "A", "name_ru": "A", "roles": ["fighter"], "lanes": ["top"], "damage_type": "Physical"},
            {"id": "B", "name": "B", "name_ru": "B", "roles": ["assassin"], "lanes": ["jungle"], "damage_type": "Physical"},
            {"id": "C", "name": "C", "name_ru": "C", "roles": ["mage"], "lanes": ["mid"], "damage_type": "Magic"},
            {"id": "D", "name": "D", "name_ru": "D", "roles": ["marksman"], "lanes": ["ad"], "damage_type": "Physical"},
            {"id": "E", "name": "E", "name_ru": "E", "roles": ["support"], "lanes": ["support"], "damage_type": "Magic"},
        ]
        snapshot = make_snapshot(champions)
        draft = [(champ, "") for champ in champions]

        original = engine._role_evidence
        calls = {"count": 0}

        def counted(*args, **kwargs):
            calls["count"] += 1
            return original(*args, **kwargs)

        engine._role_evidence = counted
        try:
            first = engine._infer_enemy_roles(draft, snapshot)
            first_calls = calls["count"]
            second = engine._infer_enemy_roles(draft, snapshot)
            self.assertGreater(first_calls, 0)
            self.assertEqual(calls["count"], first_calls)
            self.assertEqual(
                [(row[0]["id"], row[1]) for row in first],
                [(row[0]["id"], row[1]) for row in second],
            )
        finally:
            engine._role_evidence = original

    def test_item_tag_lookup_preserves_known_semantics(self):
        self.assertIn("anti_magic", engine.tags_for("Kaenic Rookern"))
        self.assertIn("anti_crit", engine.tags_for("Randuin's Omen"))
        self.assertFalse(engine.tags_for("Definitely Not An Item"))


class ItemAliasMigrationRegressionTests(unittest.TestCase):
    def test_item_alias_migration_preserves_existing_media_identity(self):
        db.init_db()
        alias = "Legacy Regression Boots"
        canonical = "Canonical Regression Boots"
        db.upsert_item(
            alias,
            "Boots",
            "regression",
            icon_url="https://example.invalid/legacy.png",
            icon_path="cache/items/legacy.png",
            tier="Upgraded",
        )
        db.upsert_media_asset(
            f"item:{alias}",
            source_url="https://example.invalid/legacy.png",
            local_path="cache/items/legacy.png",
            sha256="abc",
        )

        db.migrate_item_aliases({alias: canonical})

        self.assertIsNone(db.get_item(alias))
        row = db.get_item(canonical)
        self.assertIsNotNone(row)
        self.assertEqual(row.get("tier"), "Upgraded")
        self.assertIsNone(db.get_media_asset(f"item:{alias}"))
        self.assertIsNotNone(db.get_media_asset(f"item:{canonical}"))


class NetworkResilienceRegressionTests(unittest.TestCase):
    def test_dns_error_detector_matches_android_name_resolution_error(self):
        message = (
            "HTTPSConnectionPool(host='wrpocket.app', port=443): Max retries exceeded "
            "with url: /en/champions/garen (Caused by NameResolutionError("
            "\"Failed to resolve 'wrpocket.app' ([Errno 7] No address associated with hostname)\"))"
        )
        self.assertTrue(sources.is_dns_resolution_error(message))

    def test_two_unrelated_dns_failures_mark_network_outage(self):
        net = sources.Net(timeout=1, delay=0)
        net.note_request_error(
            "https://wrpocket.app/en/champions/garen",
            RuntimeError("Failed to resolve 'wrpocket.app'"),
        )
        self.assertFalse(net.dns_outage)
        net.note_request_error(
            "https://wildriftcore.com/en/champions/",
            RuntimeError("No address associated with hostname"),
        )
        self.assertTrue(net.dns_outage)
        self.assertEqual(
            set(net.dns_failed_hosts),
            {"wrpocket.app", "wildriftcore.com"},
        )

    def test_dns_warnings_collapse_to_one_actionable_entry(self):
        net = sources.Net(timeout=1, delay=0)
        net.note_request_error(
            "https://wrpocket.app/",
            RuntimeError("Failed to resolve 'wrpocket.app'"),
        )
        net.note_request_error(
            "https://wildriftcore.com/",
            RuntimeError("Failed to resolve 'wildriftcore.com'"),
        )
        errors = [
            "Wild Rift Pocket: NameResolutionError Failed to resolve 'wrpocket.app'",
            "WildRiftCore builds: No address associated with hostname",
            "Media refresh skipped: DNS недоступен; сохранён локальный кэш чемпионов 90/142, предметов 100/113.",
        ]
        collapsed = updater._collapse_dns_warnings(errors, net)
        self.assertEqual(len(collapsed), 1)
        self.assertIn("Network/DNS", collapsed[0])
        self.assertIn("wrpocket.app", collapsed[0])
        self.assertIn("wildriftcore.com", collapsed[0])
        self.assertIn("Media refresh skipped", collapsed[0])


class BundledDatabaseSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_initial_data()
        db.init_db()
        cls.snapshot = db.load_runtime_snapshot()

    def test_real_database_upgraded_items_have_unique_name_addressed_icon_targets(self):
        rows = [
            row for row in db.item_catalog_rows()
            if str(row.get("tier") or "").casefold() == "upgraded"
        ]
        self.assertGreaterEqual(len(rows), 50)
        primary_urls = []
        for row in rows:
            urls = sources.trusted_item_icon_urls(str(row.get("name") or ""))
            self.assertTrue(urls, str(row.get("name") or ""))
            primary_urls.append(urls[0])
        self.assertEqual(
            len(primary_urls),
            len(set(primary_urls)),
            "Two different upgraded items collapsed to the same trusted icon URL",
        )

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
