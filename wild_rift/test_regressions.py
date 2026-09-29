from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from collections import Counter

# Keep regression tests away from a developer's real runtime database.
_TEST_RUNTIME = tempfile.mkdtemp(prefix="wrca-test-")
os.environ["FLET_APP_STORAGE_DATA"] = _TEST_RUNTIME

import db
import engine
from draft_matrix_engine import DraftEdge, DraftMatrixEngine
import sources
import updater
import paths
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
    role_variants=None,
    role_situational=None,
    role_boots=None,
    role_opponent_adaptations=None,
    champion_traits=None,
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
        "role_variants": role_variants or {},
        "role_situational": role_situational or {},
        "role_boots": role_boots or {},
        "role_opponent_adaptations": role_opponent_adaptations or {},
        "champion_traits": champion_traits or {},
    }


class FreshInstallSeedRegressionTests(unittest.TestCase):
    def test_fresh_install_copies_bundled_database_verbatim(self):
        root = tempfile.mkdtemp(prefix="wrca-fresh-seed-")
        seed = paths.Path(root) / "seed.db"
        runtime = paths.Path(root) / "runtime.db"

        seed.write_bytes(b"WRCA-CANONICAL-SEED")
        paths._copy_seed_on_fresh_install(seed, runtime)

        self.assertTrue(runtime.is_file())
        self.assertEqual(runtime.read_bytes(), seed.read_bytes())

    def test_existing_runtime_is_never_overwritten_on_normal_launch(self):
        root = tempfile.mkdtemp(prefix="wrca-runtime-preserve-")
        seed = paths.Path(root) / "seed.db"
        runtime = paths.Path(root) / "runtime.db"

        seed.write_bytes(b"NEW-APK-SEED")
        runtime.write_bytes(b"UPDATED-RUNTIME-DATABASE")

        paths._copy_seed_on_fresh_install(seed, runtime)

        self.assertEqual(runtime.read_bytes(), b"UPDATED-RUNTIME-DATABASE")


class ResolverRegressionTests(unittest.TestCase):
    def test_wukong_profile_resolves_to_monkeyking_canonical_id(self):
        champs = [{
            "id": "MonkeyKing",
            "name": "Wukong",
            "name_ru": "Вуконг",
            "roles": ["Fighter"],
            "lanes": ["top", "jungle"],
            "damage_type": "Physical",
        }]
        resolve = updater.build_resolver(champs)
        self.assertEqual(resolve("wukong"), "MonkeyKing")
        self.assertEqual(resolve.exact_id("wukong"), "MonkeyKing")


class ChampionIdentityRegressionTests(unittest.TestCase):
    def test_supplement_does_not_duplicate_nunu_and_localizes_norra(self):
        base = [{
            "id": "Nunu",
            "name": "Nunu & Willump",
            "name_ru": "Нуну и Виллумп",
            "roles": ["Tank"],
            "lanes": ["jungle"],
            "damage_type": "Magic",
        }]
        supplement = [
            {
                "id": "Nunu And Willump",
                "name": "Nunu And Willump",
                "name_ru": "",
                "roles": [],
                "lanes": [],
                "damage_type": "",
                "profile_slug": "nunu-and-willump",
            },
            {
                "id": "Norra",
                "name": "Norra",
                "name_ru": "",
                "roles": [],
                "lanes": [],
                "damage_type": "",
                "profile_slug": "norra",
            },
        ]

        merged = updater.merge_champion_roster_supplement(base, supplement)
        self.assertEqual([row["id"] for row in merged], ["Nunu", "Norra"])
        norra = next(row for row in merged if row["id"] == "Norra")
        self.assertEqual(norra["name_ru"], "Норра")

    def test_duplicate_champion_identity_migrates_references(self):
        db.init_db()
        canonical = "RegressionNunu"
        alias = "Regression Nunu And Willump"
        db.upsert_champion(
            canonical, "Nunu & Willump", ["Tank"], ["jungle"], "Magic",
            "regression", name_ru="Нуну и Виллумп",
        )
        db.upsert_champion(
            alias, "Nunu And Willump", [], [], "", "regression",
            icon_url="https://example.invalid/nunu.png",
        )
        with db.connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO stats(
                   champion_id,lane,rank_segment,win_rate,pick_rate,ban_rate,date
                   ) VALUES(?,?,?,?,?,?,?)""",
                (alias, "jungle", "all", 51.0, 5.0, 2.0, "2026-09-27"),
            )
            con.execute(
                """INSERT OR REPLACE INTO counter_items(
                   enemy_id,item_name,reason,source
                   ) VALUES(?,?,?,?)""",
                (alias, "Thornmail", "regression", "regression"),
            )

        db.migrate_champion_identities({alias: canonical})

        with db.connect() as con:
            self.assertIsNone(
                con.execute("SELECT 1 FROM champions WHERE id=?", (alias,)).fetchone()
            )
            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM stats WHERE champion_id=? AND lane='jungle'",
                    (canonical,),
                ).fetchone()
            )
            self.assertIsNotNone(
                con.execute(
                    "SELECT 1 FROM counter_items WHERE enemy_id=? AND item_name='Thornmail'",
                    (canonical,),
                ).fetchone()
            )
            con.execute("DELETE FROM counter_items WHERE enemy_id=?", (canonical,))
            con.execute("DELETE FROM stats WHERE champion_id=?", (canonical,))
            con.execute("DELETE FROM champions WHERE id=?", (canonical,))


class WildRiftCoreBuildRegressionTests(unittest.TestCase):
    def test_reader_markdown_parses_standard_variants_and_situational(self):
        text = """Title: Malphite Wild Rift Best Build Guide (Patch 7.3)

Markdown Content:
### Malphite Baron Lane — recommended build
Best Malphite build (Baron Lane): Randuin's Omen › Thornmail › Sunfire Aegis › Radiant Virtue › Amaranth's Twinguard, boots Plated Steelcaps, keystone Grasp of the Undying.

#### Standard — frontline
[Randuin's Omen](https://wildriftcore.com/en/items/randuins-omen/)›[Thornmail](https://wildriftcore.com/en/items/thornmail/)›[Sunfire Aegis](https://wildriftcore.com/en/items/sunfire-aegis/)›[Radiant Virtue](https://wildriftcore.com/en/items/radiant-virtue/)›[Amaranth's Twinguard](https://wildriftcore.com/en/items/amaranths-twinguard/)
When to pick it
Balanced draft, no strong signal
Example enemy draft
![Image: Ryze](https://example.invalid/ryze.png) ![Image: Master Yi](https://example.invalid/master-yi.png) ![Image: Lucian](https://example.invalid/lucian.png) ![Image: Kai'Sa](https://example.invalid/kaisa.png) ![Image: Lulu](https://example.invalid/lulu.png)
Nothing dominates on the other side.

#### Vs AD comps
[Randuin's Omen](https://wildriftcore.com/en/items/randuins-omen/)›[Iceborn Gauntlet](https://wildriftcore.com/en/items/iceborn-gauntlet/)›[Amaranth's Twinguard](https://wildriftcore.com/en/items/amaranths-twinguard/)›[Thornmail](https://wildriftcore.com/en/items/thornmail/)›[Unending Despair](https://wildriftcore.com/en/items/unending-despair/)
When to pick it
Mostly physical damage
Example enemy draft
![Image: Riven](https://example.invalid/riven.png) ![Image: Lee Sin](https://example.invalid/lee-sin.png) ![Image: Zed](https://example.invalid/zed.png) ![Image: Lucian](https://example.invalid/lucian.png) ![Image: Nautilus](https://example.invalid/nautilus.png)
Physical damage dominates the enemy draft.

#### Vs AP comps
[Force of Nature](https://wildriftcore.com/en/items/force-of-nature/)›[Kaenic Rookern](https://wildriftcore.com/en/items/kaenic-rookern/)›[Amaranth's Twinguard](https://wildriftcore.com/en/items/amaranths-twinguard/)›[Randuin's Omen](https://wildriftcore.com/en/items/randuins-omen/)›[Unending Despair](https://wildriftcore.com/en/items/unending-despair/)
When to pick it
Mostly magic damage
Example enemy draft
![Image: Gwen](https://example.invalid/gwen.png) ![Image: Evelynn](https://example.invalid/evelynn.png) ![Image: Syndra](https://example.invalid/syndra.png) ![Image: Kai'Sa](https://example.invalid/kaisa.png) ![Image: Karma](https://example.invalid/karma.png)
Magic damage dominates the enemy draft.

### Situational adaptations
**Mantle of the Twelfth Hour**_Against AD burst (assassins)_
**Kaenic Rookern**_Against AP burst (mages)_

### Adaptations by opponent
[**Olaf**](https://wildriftcore.com/en/champions/olaf/)›[Mantle of the Twelfth Hour](https://wildriftcore.com/en/items/mantle-of-the-twelfth-hour/)_Physical damage_
"""
        known = [
            "Randuin's Omen", "Thornmail", "Sunfire Aegis",
            "Radiant Virtue", "Amaranth's Twinguard", "Plated Steelcaps",
            "Iceborn Gauntlet", "Unending Despair", "Force of Nature",
            "Kaenic Rookern", "Mantle of the Twelfth Hour",
        ]
        payload = sources.parse_wildriftcore_build_page(text, "Malphite", known)
        self.assertEqual(len(payload["builds"]), 1)
        self.assertEqual(len(payload["variants"]), 3)
        ad = next(row for row in payload["variants"] if row["name"] == "Vs AD comps")
        self.assertEqual(ad["trigger"], "Mostly physical damage")
        self.assertIn("Iceborn Gauntlet", ad["items"])
        standard = next(
            row for row in payload["variants"]
            if row["name"] == "Standard — frontline"
        )
        self.assertIn("Ryze", standard["example_enemies"])
        self.assertIn("Master Yi", standard["example_enemies"])
        mantle_rows = [
            row for row in payload["situational"]
            if row["item"] == "Mantle of the Twelfth Hour"
        ]
        self.assertTrue(mantle_rows)
        self.assertTrue(any("Olaf" in row["trigger"] for row in mantle_rows))
        self.assertTrue(
            any(
                row["enemy"] == "Olaf"
                and row["item"] == "Mantle of the Twelfth Hour"
                for row in payload["opponent_adaptations"]
            )
        )

    def test_single_role_reader_parses_three_variants_with_example_drafts(self):
        text = """Title: Ahri Wild Rift Best Build Guide (Patch 7.3)

Markdown Content:
Best Ahri build (Mid Lane): Infinity Orb › Rabadon's Deathcap › Void Staff › Stormsurge › Hextech Rocketbelt, boots Boots of Mana, keystone Electrocute.

### Standard — AP burst
[Infinity Orb](https://wildriftcore.com/en/items/infinity-orb/)›[Rabadon's Deathcap](https://wildriftcore.com/en/items/rabadons-deathcap/)›[Void Staff](https://wildriftcore.com/en/items/void-staff/)›[Stormsurge](https://wildriftcore.com/en/items/stormsurge/)›[Hextech Rocketbelt](https://wildriftcore.com/en/items/hextech-rocketbelt/)
When to pick it
Balanced draft, no strong signal
Example enemy draft
![Image: Ryze](https://example.invalid/ryze.png) ![Image: Master Yi](https://example.invalid/masteryi.png) ![Image: Lucian](https://example.invalid/lucian.png) ![Image: Kai'Sa](https://example.invalid/kaisa.png) ![Image: Lulu](https://example.invalid/lulu.png)
Nothing dominates on the other side: the standard build maximizes your damage.
Open this draft in the tool →

### Safety — Zhonya
[Zhonya's Hourglass](https://wildriftcore.com/en/items/zhonyas-hourglass/)›[Rabadon's Deathcap](https://wildriftcore.com/en/items/rabadons-deathcap/)›[Void Staff](https://wildriftcore.com/en/items/void-staff/)›[Hextech Rocketbelt](https://wildriftcore.com/en/items/hextech-rocketbelt/)›[Infinity Orb](https://wildriftcore.com/en/items/infinity-orb/)
When to pick it
2+ dive threats 2+ burst champions
Example enemy draft
![Image: Fiora](https://example.invalid/fiora.png) ![Image: Lee Sin](https://example.invalid/leesin.png) ![Image: Aurelion Sol](https://example.invalid/asol.png) ![Image: Kai'Sa](https://example.invalid/kaisa.png) ![Image: Lulu](https://example.invalid/lulu.png)
Dive threat: Fiora, Lee Sin.
Open this draft in the tool →

### Anti-resist
[Void Staff](https://wildriftcore.com/en/items/void-staff/)›[Rabadon's Deathcap](https://wildriftcore.com/en/items/rabadons-deathcap/)›[Infinity Orb](https://wildriftcore.com/en/items/infinity-orb/)›[Stormsurge](https://wildriftcore.com/en/items/stormsurge/)›[Hextech Rocketbelt](https://wildriftcore.com/en/items/hextech-rocketbelt/)
When to pick it
2+ enemy tanks
Example enemy draft
![Image: Darius](https://example.invalid/darius.png) ![Image: Master Yi](https://example.invalid/masteryi.png) ![Image: Cho'Gath](https://example.invalid/chogath.png) ![Image: Kai'Sa](https://example.invalid/kaisa.png) ![Image: Nautilus](https://example.invalid/nautilus.png)
Against 3 tank(s): Darius, Cho'Gath, Nautilus.
Open this draft in the tool →

### Situational adaptations
**Zhonya's Hourglass**_Against AD burst (assassins, Zed, Rengar…)_

### Adaptations by opponent
[**Irelia**](https://wildriftcore.com/en/champions/irelia/)›[Zhonya's Hourglass](https://wildriftcore.com/en/items/zhonyas-hourglass/)_Physical damage_
"""
        known = [
            "Infinity Orb", "Rabadon's Deathcap", "Void Staff", "Stormsurge",
            "Hextech Rocketbelt", "Boots of Mana", "Zhonya's Hourglass",
        ]
        payload = sources.parse_wildriftcore_build_page(text, "Ahri", known)
        self.assertEqual(len(payload["variants"]), 3)
        safety = next(row for row in payload["variants"] if row["name"] == "Safety — Zhonya")
        self.assertEqual(safety["trigger"], "2+ dive threats 2+ burst champions")
        self.assertEqual(safety["example_enemies"][:2], ["Fiora", "Lee Sin"])
        self.assertIn("Dive threat: Fiora, Lee Sin.", safety["example_text"])
        irelia_rows = [
            row for row in payload["situational"]
            if "Opponent: Irelia" in str(row.get("trigger") or "")
        ]
        self.assertTrue(irelia_rows)

    def test_variant_selector_prefers_ad_variant_for_physical_team(self):
        rows = [
            {
                "variant_name": "Standard — frontline",
                "items": ["A", "B", "C", "D", "E"],
                "trigger_text": "Balanced draft, no strong signal",
                "priority": 0,
            },
            {
                "variant_name": "Vs AD comps",
                "items": ["F", "G", "H", "I", "J"],
                "trigger_text": "Mostly physical damage",
                "priority": 1,
            },
            {
                "variant_name": "Vs AP comps",
                "items": ["K", "L", "M", "N", "O"],
                "trigger_text": "Mostly magic damage",
                "priority": 2,
            },
        ]
        chosen = engine._select_role_variant(
            rows,
            engine.Counter({"anti_physical": 4, "anti_magic": 1}),
        )
        self.assertEqual(chosen["variant_name"], "Vs AD comps")

    def test_standard_ap_burst_title_is_not_enemy_ap_signal(self):
        rows = [
            {
                "variant_name": "Standard — AP burst",
                "items": ["A", "B", "C", "D", "E"],
                "trigger_text": "Balanced draft, no strong signal",
                "example_enemies": ["Ryze", "Master Yi", "Lucian", "Kai'Sa", "Lulu"],
                "example_text": "Nothing dominates on the other side.",
                "priority": 0,
            },
            {
                "variant_name": "Safety — Zhonya",
                "items": ["F", "G", "H", "I", "J"],
                "trigger_text": "2+ dive threats 2+ burst champions",
                "example_enemies": ["Fiora", "Lee Sin", "Aurelion Sol", "Kai'Sa", "Lulu"],
                "example_text": "Dive threat: Fiora, Lee Sin.",
                "priority": 1,
            },
        ]
        chosen = engine._select_role_variant(
            rows,
            engine.Counter({"anti_magic": 4}),
        )
        self.assertEqual(chosen["variant_name"], "Standard — AP burst")

    def test_variant_selector_uses_two_dive_threats(self):
        rows = [
            {
                "variant_name": "Standard — AP burst",
                "items": ["A", "B", "C", "D", "E"],
                "trigger_text": "Balanced draft, no strong signal",
                "example_enemies": ["Ryze", "Master Yi", "Lucian", "Kai'Sa", "Lulu"],
                "example_text": "Nothing dominates on the other side.",
                "priority": 0,
            },
            {
                "variant_name": "Safety — Zhonya",
                "items": ["F", "G", "H", "I", "J"],
                "trigger_text": "2+ dive threats 2+ burst champions",
                "example_enemies": ["Fiora", "Lee Sin", "Aurelion Sol", "Kai'Sa", "Lulu"],
                "example_text": "Dive threat: Fiora, Lee Sin.",
                "priority": 1,
            },
        ]
        chosen = engine._select_role_variant(
            rows,
            engine.Counter({"anti_dive": 2, "anti_burst": 1}),
        )
        self.assertEqual(chosen["variant_name"], "Safety — Zhonya")

    def test_variant_selector_ignores_allied_only_rule_without_allies(self):
        rows = [
            {
                "variant_name": "Standard — support",
                "items": ["A", "B", "C", "D", "E"],
                "trigger_text": "Balanced draft, no strong signal",
                "example_enemies": ["Ryze", "Master Yi", "Lucian", "Kai'Sa", "Lulu"],
                "example_text": "Nothing dominates on the other side.",
                "priority": 0,
            },
            {
                "variant_name": "Tempo & engage",
                "items": ["F", "G", "H", "I", "J"],
                "trigger_text": "2+ allied engages No enemy tank",
                "example_enemies": ["Ryze", "Master Yi", "Lucian", "Kai'Sa", "Lulu"],
                "example_text": "Allied engage: Pantheon, Lee Sin.",
                "priority": 1,
            },
        ]
        chosen = engine._select_role_variant(
            rows,
            engine.Counter({"anti_engage": 5, "anti_cc": 5}),
        )
        self.assertEqual(chosen["variant_name"], "Standard — support")

    def test_wrc_example_explanation_learns_dive_enemy_tag(self):
        fiora = {
            "id": "Fiora", "name": "Fiora", "name_ru": "Фиора",
            "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical",
        }
        snapshot = make_snapshot(
            [fiora],
            role_variants={
                ("Ahri", "Мид"): [{
                    "variant_name": "Safety — Zhonya",
                    "items": ["A", "B", "C", "D", "E"],
                    "trigger_text": "2+ dive threats 2+ burst champions",
                    "example_enemies": ["Fiora", "Lee Sin", "Aurelion Sol", "Kai'Sa", "Lulu"],
                    "example_text": "Dive threat: Fiora, Lee Sin.",
                    "priority": 1,
                }]
            },
        )
        tags = engine._wrc_example_threat_tags(fiora, snapshot)
        self.assertIn("anti_dive", tags)

    def test_metadata_only_variant_selects_rule_without_replacing_standard_core(self):
        rows = [
            {
                "variant_name": "Standard — bruiser",
                "items": ["A", "B", "C", "D", "E"],
                "trigger_text": "Balanced draft, no strong signal",
                "priority": 0,
            },
            {
                "variant_name": "Anti-tank — shred",
                "items": [],
                "trigger_text": "2+ enemy tanks",
                "priority": 1,
            },
            {
                "variant_name": "Sustain — brawler",
                "items": [],
                "trigger_text": "2+ duelists",
                "priority": 2,
            },
        ]
        chosen = engine._select_role_variant(
            rows, Counter({"anti_tank": 2})
        )
        self.assertEqual(chosen["variant_name"], "Anti-tank — shred")
        self.assertEqual(chosen["items"], [])

    def test_mostly_physical_variant_uses_three_of_five_majority(self):
        rows = [
            {
                "variant_name": "Standard — frontline",
                "items": [],
                "trigger_text": "Balanced draft, no strong signal",
                "priority": 0,
            },
            {
                "variant_name": "Vs AD comps",
                "items": [],
                "trigger_text": "Mostly physical damage",
                "priority": 1,
            },
            {
                "variant_name": "Vs AP comps",
                "items": [],
                "trigger_text": "Mostly magic damage",
                "priority": 2,
            },
        ]
        chosen = engine._select_role_variant(
            rows,
            Counter({"anti_physical": 3, "anti_magic": 2}),
        )
        self.assertEqual(chosen["variant_name"], "Vs AD comps")

    def test_wrc_counter_trait_profile_marks_duelist(self):
        jax = {
            "id": "Jax", "name": "Jax", "name_ru": "Джакс",
            "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical",
        }
        snapshot = make_snapshot(
            [jax],
            champion_traits={
                "Jax": [{
                    "champion_id": "Jax",
                    "trait": "duelist",
                    "confidence": 0.28,
                    "evidence_count": 7,
                    "mentions": 25,
                    "source": "wildriftcore.com",
                }]
            },
        )
        self.assertIn(
            "anti_duelist",
            engine._wrc_counter_trait_tags(jax, snapshot),
        )

    def test_heavy_mobility_generic_rule_requires_two_enemies(self):
        tags = {"anti_mobility"}
        self.assertFalse(engine._trigger_is_active(
            "Against heavy mobility", tags, Counter({"anti_mobility": 1})
        ))
        self.assertTrue(engine._trigger_is_active(
            "Against heavy mobility", tags, Counter({"anti_mobility": 2})
        ))

    def test_db_persists_role_build_variants(self):
        db.init_db()
        cid = "RegressionVariantChampion"
        db.upsert_champion(
            cid, cid, ["Tank"], ["top"], "Magic", "regression"
        )
        db.replace_source_role_builds_partial(
            "wildriftcore.com",
            [(cid, "Барон", ["Randuin's Omen", "Thornmail", "Sunfire Aegis"], "Plated Steelcaps", "7.3", "https://example.invalid")],
            [],
            [],
            [(cid, "Барон", "Vs AD comps", ["Randuin's Omen", "Thornmail", "Sunfire Aegis", "Radiant Virtue", "Amaranth's Twinguard"], "Mostly physical damage", 1, "7.3", "https://example.invalid")],
        )
        rows = db.get_role_build_variants(cid, "Барон")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["variant_name"], "Vs AD comps")
        self.assertEqual(rows[0]["items"][0], "Randuin's Omen")
        with db.connect() as con:
            con.execute("DELETE FROM role_build_variants WHERE champion_id=?", (cid,))
            con.execute("DELETE FROM role_builds WHERE champion_id=?", (cid,))
            con.execute("DELETE FROM champions WHERE id=?", (cid,))


class RecommendationRegressionTests(unittest.TestCase):
    def test_visible_total_score_is_the_primary_pick_order(self):
        champions = [
            {"id": "Malphite", "name": "Malphite", "name_ru": "Мальфит", "roles": ["Tank"], "lanes": ["top"], "damage_type": "Magic"},
            {"id": "Jax", "name": "Jax", "name_ru": "Джакс", "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical"},
            {"id": "Irelia", "name": "Irelia", "name_ru": "Ирелия", "roles": ["Fighter"], "lanes": ["top"], "damage_type": "Physical"},
        ]
        core = [
            "Randuin's Omen", "Thornmail", "Sunfire Aegis",
            "Radiant Virtue", "Amaranth's Twinguard",
        ]
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
        role_builds = {
            (cid, "Барон"): {
                "champion_id": cid,
                "role": "Барон",
                "items": core,
                "boot_name": boot,
                "source": "wildriftcore.com",
                "source_url": f"https://example.invalid/{cid}/builds",
            }
            for cid in ("Malphite", "Jax")
        }
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
            items=items,
            role_builds=role_builds,
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

    def test_missing_role_build_is_rejected_instead_of_wrpocket_fallback(self):
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

        picks = engine.recommend_picks(
            "Барон", [("Irelia", "Барон")], limit=8, snapshot=snapshot
        )
        self.assertNotIn(
            "Malphite",
            [row["champion"]["id"] for row in picks],
        )

        with self.assertRaisesRegex(ValueError, "WildRiftCore"):
            engine.recommend_build(
                "Malphite", [("Irelia", "Барон")],
                role_ru="Барон", snapshot=snapshot,
            )

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

    def test_exact_wrc_opponent_adaptation_outranks_generic_rule(self):
        champions = [
            {"id": "Ahri", "name": "Ahri", "name_ru": "Ари", "roles": ["Mage"], "lanes": ["mid"], "damage_type": "Magic"},
            {"id": "Irelia", "name": "Irelia", "name_ru": "Ирелия", "roles": ["Fighter"], "lanes": ["mid"], "damage_type": "Physical"},
        ]
        core = [
            "Infinity Orb", "Rabadon's Deathcap", "Void Staff",
            "Stormsurge", "Hextech Rocketbelt",
        ]
        boot = "Boots of Mana"
        zhonya = "Zhonya's Hourglass"
        items = {
            name: {
                "name": name,
                "category": "Boots" if name == boot else "Magic",
                "tier": "Upgraded",
                "stats_json": "[]",
                "effect_en": "",
            }
            for name in [*core, boot, zhonya]
        }
        snapshot = make_snapshot(
            champions,
            items=items,
            role_builds={
                ("Ahri", "Мид"): {
                    "champion_id": "Ahri",
                    "role": "Мид",
                    "items": core,
                    "boot_name": boot,
                    "source": "wildriftcore.com",
                    "source_url": "https://wildriftcore.com/en/champions/ahri/builds/",
                }
            },
            role_variants={
                ("Ahri", "Мид"): [{
                    "variant_name": "Standard — AP burst",
                    "items": core,
                    "trigger_text": "Balanced draft, no strong signal",
                    "example_enemies": ["Ryze", "Master Yi", "Lucian", "Kai'Sa", "Lulu"],
                    "example_text": "Nothing dominates on the other side.",
                    "priority": 0,
                }]
            },
            role_situational={
                ("Ahri", "Мид"): [{
                    "item_name": zhonya,
                    "trigger_text": "Against AD burst",
                    "priority": 1,
                    "source": "wildriftcore.com",
                }]
            },
            role_opponent_adaptations={
                ("Ahri", "Мид"): [{
                    "enemy_name": "Irelia",
                    "enemy_name_norm": db.normalize_search("Irelia"),
                    "item_name": zhonya,
                    "reason": "Physical damage",
                    "priority": 100,
                    "source": "wildriftcore.com",
                }]
            },
        )

        build = engine.recommend_build(
            "Ahri", [("Irelia", "Мид")], role_ru="Мид", snapshot=snapshot
        )
        self.assertIn(zhonya, build["ordered"])
        self.assertTrue(build["exact_opponent_adaptations"])
        self.assertEqual(
            build["exact_opponent_adaptations"][0]["enemy"], "Irelia"
        )
        self.assertEqual(
            build["exact_opponent_adaptations"][0]["item"], zhonya
        )

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


class WildRiftCoreReaderCoverageRegressionTests(unittest.TestCase):
    def test_current_seo_counter_cards_parse_general_edges_and_traits(self):
        text = """Title: Garen Wild Rift Counters Guide

Markdown Content:
## Hard Counters
### [Vayne](https://wildriftcore.com/en/champions/vayne/)+3

Mobility Burst[Vayne build →](https://wildriftcore.com/en/champions/vayne/builds/)See details

## Favorable
### [Dr. Mundo](https://wildriftcore.com/en/champions/dr-mundo/)−1

Tanks Sustain[Dr. Mundo build →](https://wildriftcore.com/en/champions/dr-mundo/builds/)See details
"""
        mapping = {
            "vayne": "Vayne",
            "drmundo": "DrMundo",
        }
        def resolve(value):
            return mapping.get(sources.slugish(value))

        rows = sources._parse_wildriftcore_counter_page(
            text, "Garen", resolve
        )
        self.assertIn(("Garen", "Vayne", "", -3.0), rows)
        self.assertIn(("Garen", "DrMundo", "", 1.0), rows)

        traits = sources._parse_wildriftcore_counter_traits(text, resolve)
        self.assertIn(("Vayne", "mobility"), traits)
        self.assertIn(("Vayne", "burst"), traits)
        self.assertIn(("DrMundo", "tank"), traits)
        self.assertIn(("DrMundo", "healing"), traits)

    def test_reader_markdown_counter_table_parses_role_edges(self):
        text = """
Title: Malphite Wild Rift Counters Guide
Markdown Content:
## How to counter Malphite in Top: the essentials

Matchup | Verdict | Edge | Win rate | Pick rate
--- | --- | --- | --- | ---
Olaf | Hard Counters | +3 | 49.2% | 1.1%
Irelia | Favorable | −1 | 47.4% | 1.8%

## Unfavorable
"""
        mapping = {"olaf": "Olaf", "irelia": "Irelia"}
        resolve = lambda value: mapping.get(sources.slugish(value))
        rows = sources._parse_wildriftcore_counter_page(
            text, "Malphite", resolve
        )
        self.assertIn(("Malphite", "Olaf", "Барон", -3.0), rows)
        self.assertIn(("Malphite", "Irelia", "Барон", 1.0), rows)

    def test_reader_markdown_tier_page_parses_cards_and_integrity_links(self):
        text = """
Title: Wild Rift Baron Lane Tier List
Markdown Content:
## Every Baron Lane champion ranked, S+ to C

S+

[S+ Malphite 58.1% WR 8.6% PR](https://wildriftcore.com/en/champions/malphite/)
[S+ Jax 52.6% WR 4.0% PR](https://wildriftcore.com/en/champions/jax/)

S

[S Volibear 50.1% WR 7.1% PR](https://wildriftcore.com/en/champions/volibear/)

## How do we calculate this tier list?
"""
        mapping = {
            "malphite": "Malphite",
            "jax": "Jax",
            "volibear": "Volibear",
        }
        resolve = lambda value: mapping.get(sources.slugish(value))
        rows = sources._parse_wildriftcore_tier_page(
            text, "Барон", resolve
        )
        self.assertEqual(
            {(cid, role, tier) for cid, role, tier in rows},
            {
                ("Malphite", "Барон", "S+"),
                ("Jax", "Барон", "S+"),
                ("Volibear", "Барон", "S"),
            },
        )
        expected, unresolved = sources._wildriftcore_ranked_section_ids(
            text, resolve
        )
        self.assertEqual(expected, {"Malphite", "Jax", "Volibear"})
        self.assertFalse(unresolved)


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


class BootsOfManaLocalizationRegressionTests(unittest.TestCase):
    def test_boots_of_mana_has_russian_name(self):
        self.assertEqual(
            sources.item_name_ru("Boots of Mana"),
            "Сапоги маны",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DraftMatrixEngineRegressionTests(unittest.TestCase):
    def test_full_positive_matrix_row_is_perfect_matchup_and_coverage(self):
        matrix = DraftMatrixEngine()
        row = [
            DraftEdge(str(i), f"E{i}", "", 3.0, 1.0)
            for i in range(5)
        ]
        result = matrix.analyze_row(row)
        self.assertAlmostEqual(result["matchup_score"], 100.0)
        self.assertAlmostEqual(result["coverage_score"], 100.0)
        self.assertEqual(result["coverage_count"], 5)

    def test_lane_opponent_is_weighted_in_matchup_and_coverage(self):
        matrix = DraftMatrixEngine()
        row = [
            DraftEdge("lane", "Lane", "Мид", 3.0, matrix.lane_weight("Мид", "Мид")),
            DraftEdge("a", "A", "Барон", 0.0, matrix.lane_weight("Мид", "Барон")),
            DraftEdge("b", "B", "Лес", 0.0, matrix.lane_weight("Мид", "Лес")),
            DraftEdge("c", "C", "ADC", 0.0, matrix.lane_weight("Мид", "ADC")),
            DraftEdge("d", "D", "Саппорт", 0.0, matrix.lane_weight("Мид", "Саппорт")),
        ]
        result = matrix.analyze_row(row)
        # 2 weighted positive shares out of total draft weight 6.
        self.assertAlmostEqual(result["coverage_score"], 100.0 / 3.0, places=5)
        # Weighted edge: (3/3 * 2) / 6 = 1/3 -> score 66.666...
        self.assertAlmostEqual(result["matchup_score"], 200.0 / 3.0, places=5)

    def test_inverse_matrix_edge_penalizes_candidate(self):
        matrix = DraftMatrixEngine()
        result = matrix.analyze_row([
            DraftEdge("enemy", "Enemy", "Барон", -3.0, 2.0),
        ])
        self.assertAlmostEqual(result["matchup_score"], 0.0)
        self.assertAlmostEqual(result["coverage_score"], 0.0)
        self.assertEqual(result["negative"], ["Enemy"])

    def test_final_formula_is_exact_60_20_15_5(self):
        matrix = DraftMatrixEngine()
        result = matrix.final_score(
            matchup_score=80.0,
            coverage_score=60.0,
            tier_score=100.0,
            winrate_score=40.0,
        )
        self.assertAlmostEqual(result["matchup"], 48.0)
        self.assertAlmostEqual(result["coverage"], 12.0)
        self.assertAlmostEqual(result["tier"], 15.0)
        self.assertAlmostEqual(result["winrate"], 2.0)
        self.assertAlmostEqual(result["score"], 77.0)

    def test_build_feature_vector_uses_same_draft_signal_names(self):
        matrix = DraftMatrixEngine()
        features = matrix.feature_vector(
            Counter({
                "anti_tank": 2,
                "anti_dive": 3,
                "anti_burst": 2,
                "anti_magic": 1,
                "anti_cc": 2,
            })
        )
        self.assertEqual(features["tanks"], 2)
        self.assertEqual(features["dive"], 3)
        self.assertEqual(features["burst"], 2)
        self.assertEqual(features["magic"], 1)
        self.assertEqual(features["cc"], 2)
