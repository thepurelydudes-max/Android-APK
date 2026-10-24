"""Apply only Riot's 7.3b balance fields represented in the WRCA SQLite data.

The current release is the source of truth. No matchup scores, role builds,
tier lists, cache pages, champion identities or icon files are recalculated.
7.3c changes only AAA ARAM, which WRCA does not model.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3

# (old official price, new official price); a stored 0 means "not collected".
ITEM_PRICES = {
    "Youmuu's Ghostblade": (3000, 2900),
    "Locket of the Iron Solari": (2600, 2500),
    "Thornmail": (2700, 2850),
    "Yun Tal Wildarrows": (3100, 3000),
}

RUNE_TEXTS = {
    "Electrocute": (
        "Three separate hits on one champion deal 60–210 adaptive damage (based on level) + 10% bonus AD + 5% AP.",
        "Три отдельных попадания по одному чемпиону наносят 60–210 адаптивного урона (зависит от уровня) + 10% дополнительной силы атаки + 5% силы умений.",
    ),
    "Cheap Shot": (
        "Deals bonus true damage against crowd-controlled champions. Cooldown: 5 seconds.",
        "Наносит дополнительный чистый урон чемпионам под действием контроля. Перезарядка: 5 секунд.",
    ),
    "Grasp of Undying": (
        "After time in combat, the empowered attack heals and deals damage equal to 3% max Health; permanently grants 8 max Health on trigger.",
        "После некоторого времени в бою усиленная атака лечит и наносит урон в размере 3% максимального здоровья; срабатывание навсегда дает 8 максимального здоровья.",
    ),
    "Aery": (
        "Damaging an enemy sends Aery to damage them; helping an ally shields them for 20–100 (based on level) + 10% bonus AD + 5% AP.",
        "При нанесении урона Пушинка атакует врага, а при помощи союзнику дает щит величиной 20–100 (зависит от уровня) + 10% дополнительной силы атаки + 5% силы умений.",
    ),
}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    args = p.parse_args()

    with sqlite3.connect(args.db) as con:
        con.row_factory = sqlite3.Row
        before = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("champions", "matchups", "items", "role_builds",
                          "role_runes", "runes", "champion_tiers")
        }
        summary: dict[str, object] = {"updated_items": [], "updated_runes": []}

        for name, (old_price, new_price) in ITEM_PRICES.items():
            row = con.execute(
                "SELECT name,price,stats_json,effect_en FROM items WHERE name=?",
                (name,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Cannot patch missing existing item: {name}")
            if row["price"] not in (0, old_price, new_price):
                raise RuntimeError(f"Unexpected price for {name}: {row['price']}")
            con.execute(
                "UPDATE items SET price=?,data_patch='7.3b' WHERE name=?",
                (new_price, name),
            )
            summary["updated_items"].append(name)

        # Locket's only changed base stat: +200 -> +300 HP.
        locket = "Locket of the Iron Solari"
        row = con.execute("SELECT stats_json FROM items WHERE name=?", (locket,)).fetchone()
        stats = json.loads(row["stats_json"])
        if not isinstance(stats, list):
            raise RuntimeError("Invalid Locket stats JSON")
        old_hp = [s for s in stats if re.fullmatch(r"\\+200\\s+(?:HP|Health)", str(s), re.I)]
        new_hp = [s for s in stats if re.fullmatch(r"\\+300\\s+(?:HP|Health)", str(s), re.I)]
        if len(old_hp) == 1:
            stats = [re.sub(r"200", "300", s, count=1) if s == old_hp[0] else s for s in stats]
            con.execute("UPDATE items SET stats_json=? WHERE name=?",
                        (json.dumps(stats, ensure_ascii=False), locket))
        elif not (len(new_hp) == 1 and len(old_hp) == 0):
            raise RuntimeError(f"Cannot confirm old/new Locket Health stat: {stats!r}")

        # The currently published source is stale: Warmog reads 950 although
        # Riot's official immediate pre-7.3b threshold was 900. Both values
        # must be replaced with the verified 7.3b value (1250), never guessed.
        warmog = "Warmog's Armor"
        row = con.execute("SELECT effect_en FROM items WHERE name=?", (warmog,)).fetchone()
        if row is None or not row["effect_en"]:
            raise RuntimeError("Warmog effect is missing")
        effect = row["effect_en"]
        if re.search(r"\\b(?:900|950)\\s+bonus health\\b", effect, re.I):
            effect = re.sub(r"\\b(?:900|950)\\s+bonus health\\b",
                            "1250 bonus health", effect, flags=re.I)
        elif not re.search(r"\\b1250\\s+bonus health\\b", effect, re.I):
            raise RuntimeError("Cannot locate Warmog trigger threshold")
        con.execute(
            "UPDATE items SET effect_en=?,data_patch='7.3b' WHERE name=?",
            (effect, warmog),
        )
        summary["updated_items"].append(warmog)

        # The existing 7.3a package has no Statikk Shiv row or icon. Do not
        # silently insert an iconless incomplete item as a patch-note update.
        count = con.execute(
            "SELECT COUNT(*) FROM items WHERE name='Statikk Shiv'"
        ).fetchone()[0]
        summary["statikk_missing_from_baseline"] = count == 0
        if count:
            raise RuntimeError(
                "Statikk Shiv already exists; inspect and update its 7.3b "
                "price and Energized stacks explicitly"
            )

        # Must run AFTER merging the 7.3a rune seed and localization, otherwise
        # the old seed will overwrite these four new 7.3b descriptions.
        for name, (en, ru) in RUNE_TEXTS.items():
            row = con.execute("SELECT name FROM runes WHERE name=?", (name,)).fetchone()
            if row is None:
                raise RuntimeError(f"Missing baseline rune: {name}")
            con.execute(
                "UPDATE runes SET effect_en=?,effect_ru=?,patch='7.3b' "
                "WHERE name=?",
                (en, ru, name),
            )
            summary["updated_runes"].append(name)

        con.execute(
            "INSERT INTO meta(key,value) VALUES('official_standard_balance_patch','7.3b') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        con.execute(
            "INSERT INTO meta(key,value) VALUES('balance_delta_note', "
            "'7.3a -> 7.3b: 8 champion abilities (not stored in WRCA); "
            "5 existing item records and 4 rune descriptions updated; "
            "Statikk Shiv not in baseline; matchup scores unchanged. "
            "7.3c AAA ARAM only.') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

        after = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        if before != after:
            raise RuntimeError(f"Unexpected changed row counts: {before} -> {after}")
        quick = con.execute("PRAGMA quick_check").fetchone()[0]
        if quick != "ok":
            raise RuntimeError(f"SQLite check failed: {quick}")
        con.commit()
        summary["row_counts_unchanged"] = before
        print("VERIFIED_7_3B_DELTA:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
