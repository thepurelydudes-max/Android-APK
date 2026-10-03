from __future__ import annotations

"""Experimental WRCA 3.9.3 rune pilot.

This module is deliberately isolated from the production database/update
protocol. It exists only to evaluate whether draft-aware rune recommendations
are useful enough to keep. If the experiment is rejected, removing this module
and the small UI hook removes the feature without touching production item or
matchup logic.

Pilot source: WildRiftFire patch 7.3a champion guides, checked 2026-10-03.
"""

from typing import Any


PILOT_PATCH = "7.3a"
PILOT_SOURCE = "WildRiftFire"

# Keys are (champion id casefold, WRCA role).
# "situational" rows replace one rune when a draft condition is active.
RUNE_PAGES: dict[tuple[str, str], dict[str, Any]] = {
    ("gnar", "Барон"): {
        "base": ["Fleet Footwork", "Demolish", "Bone Plating", "Perseverance", "Brutal"],
        "situational": [
            {"condition": "poke", "from": "Bone Plating", "to": "Second Wind", "reason_ru": "против poke"},
        ],
    },
    ("lee sin", "Лес"): {
        "base": ["Conqueror", "Battle Zeal", "Cut Down", "Legend: Alacrity", "Absolute Focus"],
        "situational": [
            {"condition": "squishies", "from": "Cut Down", "to": "Coup de Grace", "reason_ru": "против хрупкого состава"},
        ],
    },
    ("vi", "Лес"): {
        "base": ["Conqueror", "Triumph", "Cut Down", "Legend: Alacrity", "Sudden Impact"],
        "situational": [
            {"condition": "cc", "from": "Sudden Impact", "to": "Perseverance", "reason_ru": "против большого количества контроля"},
        ],
    },
    ("norra", "Мид"): {
        "base": ["First Strike", "Sudden Impact", "Tyrant", "Eyeball Collector", "Bone Plating"],
        "situational": [
            {"condition": "tanks", "from": "Bone Plating", "to": "Cut Down", "reason_ru": "против танков"},
        ],
    },
    ("vex", "Мид"): {
        "base": ["Electrocute", "Cheap Shot", "Chain Assault", "Eyeball Collector", "Bone Plating"],
        "situational": [],
    },
    ("yunara", "ADC"): {
        "base": ["Lethal Tempo", "Brutal", "Cut Down", "Legend: Alacrity", "Bone Plating"],
        "situational": [
            {"condition": "squishies", "from": "Cut Down", "to": "Coup de Grace", "reason_ru": "против хрупкого состава"},
        ],
    },
    ("rakan", "Саппорт"): {
        "base": ["Ice Overlord", "Unshakeable", "Bone Plating", "Perseverance", "Sudden Impact"],
        "situational": [],
    },
    ("malphite", "Барон"): {
        "base": ["Grasp of the Undying", "Unshakeable", "Second Wind", "Overgrowth", "Empowered Attack"],
        "situational": [
            {"condition": "cc", "from": "Overgrowth", "to": "Perseverance", "reason_ru": "против большого количества контроля"},
        ],
    },
    ("malphite", "Саппорт"): {
        "base": ["Arcane Comet", "Unshakeable", "Second Wind", "Overgrowth", "Transcendence"],
        "situational": [
            {"condition": "cc", "from": "Overgrowth", "to": "Perseverance", "reason_ru": "против большого количества контроля"},
        ],
    },
    ("urgot", "Барон"): {
        "base": ["Empowerment", "Demolish", "Second Wind", "Overgrowth", "Brutal"],
        "situational": [
            {"condition": "cc", "from": "Overgrowth", "to": "Perseverance", "reason_ru": "против большого количества контроля"},
        ],
    },
    ("garen", "Барон"): {
        "base": ["Grasp of the Undying", "Unshakeable", "Second Wind", "Perseverance", "Nimbus Cloak"],
        "situational": [],
    },
}


def _active_conditions(profile: dict | None) -> set[str]:
    profile = profile or {}
    counts = profile.get("counts") or {}
    enemy_count = int(profile.get("enemy_count") or 0)

    tank_count = int(counts.get("anti_tank") or 0)
    cc_count = int(counts.get("anti_cc") or 0)
    poke_count = int(counts.get("anti_poke") or 0)

    active: set[str] = set()
    # With one/two selected enemies, treat the input as a lane/matchup signal.
    # With a fuller draft, require a team-level pattern before replacing a rune.
    if poke_count >= (1 if enemy_count <= 2 else 2):
        active.add("poke")
    if cc_count >= (1 if enemy_count <= 2 else 2):
        active.add("cc")
    if tank_count >= (1 if enemy_count <= 2 else 2):
        active.add("tanks")
    if enemy_count >= 3 and tank_count <= 1:
        active.add("squishies")
    return active


def recommend_runes(champion_id: str, role: str, threat_profile: dict | None) -> dict | None:
    row = RUNE_PAGES.get((str(champion_id or "").casefold(), str(role or "")))
    if not row:
        return None

    selected = list(row.get("base") or [])
    changes: list[dict] = []
    active = _active_conditions(threat_profile)

    for rule in row.get("situational") or []:
        if str(rule.get("condition") or "") not in active:
            continue
        old = str(rule.get("from") or "")
        new = str(rule.get("to") or "")
        if old and new and old in selected and new not in selected:
            selected[selected.index(old)] = new
            changes.append({
                "from": old,
                "to": new,
                "reason_ru": str(rule.get("reason_ru") or ""),
            })

    return {
        "base": list(row.get("base") or []),
        "selected": selected,
        "changes": changes,
        "patch": PILOT_PATCH,
        "source": PILOT_SOURCE,
        "pilot": True,
    }
