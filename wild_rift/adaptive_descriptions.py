from __future__ import annotations

import json
import re

import engine
from sources import clean_wrpocket_item_effect as clean_item_effect, clean_wrpocket_item_stats as clean_item_stats

_REASON_TEXT = {
    "ru": {
        "direct": "ситуационный контр-предмет против {enemies}",
        "anti_heal": "антихил против {enemies}: снижает эффективность их лечения и восстановления здоровья",
        "anti_shield": "полезен против щитов {enemies}",
        "anti_magic": "защищает от магического урона {enemies}",
        "anti_physical": "защищает от физического урона {enemies}",
        "anti_auto": "снижает давление частых автоатак {enemies}",
        "anti_crit": "особенно полезен против критических атак {enemies}",
        "anti_burst": "помогает пережить взрывной урон {enemies}",
        "anti_tank": "помогает разбирать прочные цели: {enemies}",
        "anti_cc": "полезен против контроля {enemies}",
        "anti_magic_resist": "помогает пробивать сопротивление магии у {enemies}",
        "core": "базовый сильный предмет из обычной сборки героя",
    },
    "en": {
        "direct": "situational counter-item against {enemies}",
        "anti_heal": "anti-heal against {enemies}: reduces their healing and health regeneration",
        "anti_shield": "useful against shields from {enemies}",
        "anti_magic": "helps protect against magic damage from {enemies}",
        "anti_physical": "helps protect against physical damage from {enemies}",
        "anti_auto": "reduces pressure from repeated basic attacks from {enemies}",
        "anti_crit": "especially useful against critical strikes from {enemies}",
        "anti_burst": "helps survive burst damage from {enemies}",
        "anti_tank": "helps deal with durable targets: {enemies}",
        "anti_cc": "useful against crowd control from {enemies}",
        "anti_magic_resist": "helps penetrate magic resistance on {enemies}",
        "core": "strong core item from the hero's standard build",
    },
}

def _item_stats_list(item: dict | None) -> list[str]:
    if not item:
        return []
    raw_stats = item.get("stats_json") or "[]"
    if isinstance(raw_stats, str):
        try:
            stats = json.loads(raw_stats)
        except Exception:
            stats = []
    else:
        stats = list(raw_stats or [])
    return clean_item_stats(stats)

def _has_stat(stats: list[str], *needles: str) -> bool:
    folded = " ".join(stats).casefold()
    return any(str(x).casefold() in folded for x in needles)

def _canonical_reason_kind(item: str, kind: str) -> str:
    """Normalize overlapping reason sources before text is generated.

    Direct counter rows and generic threat tags often describe the same reason.
    Canonicalising *before* grouping prevents duplicated phrases such as
    "physical damage Aatrox" appearing twice for the same item.
    """
    value = str(kind or "direct")
    aliases = {
        "anti_attack_speed": "anti_auto",
        "anti_armor": "anti_tank",
    }
    value = aliases.get(value, value)
    if value != "direct":
        return value

    tags = engine.tags_for(item)
    for candidate in (
        "anti_heal", "anti_shield", "anti_crit", "anti_magic",
        "anti_physical", "anti_auto", "anti_attack_speed", "anti_burst",
        "anti_cc", "anti_tank", "anti_armor", "anti_magic_resist",
    ):
        if candidate in tags:
            return aliases.get(candidate, candidate)
    return "direct"

def _group_build_reason_details(item: str, build: dict, champion_name) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    details = list((build.get("reason_details") or {}).get(item, []) or [])

    # If the same enemy already has a precise reason, that reason wins over a
    # generic source-level "direct counter" marker.  This avoids nonsense like
    # calling Mortal Reminder anti-heal against Blitzcrank when the actual
    # contextual reason is that he is a durable target.
    specific_by_enemy: dict[str, set[str]] = {}
    for detail in details:
        raw_kind = str(detail.get("kind") or "direct")
        enemy_raw = str(detail.get("enemy") or "")
        if raw_kind != "direct" and enemy_raw:
            specific_by_enemy.setdefault(enemy_raw, set()).add(_canonical_reason_kind(item, raw_kind))

    for detail in details:
        raw_kind = str(detail.get("kind") or "direct")
        enemy_raw = str(detail.get("enemy") or "")
        if raw_kind == "direct" and enemy_raw and specific_by_enemy.get(enemy_raw):
            continue
        kind = _canonical_reason_kind(item, raw_kind)
        enemy = champion_name(enemy_raw) if enemy_raw else ""
        grouped.setdefault(kind, [])
        if enemy and enemy not in grouped[kind]:
            grouped[kind].append(enemy)
    return grouped

def _localized_build_item_reason(item: str, build: dict, lang: str, champion_name) -> str:
    language = "ru" if str(lang).lower().startswith("ru") else "en"
    grouped = _group_build_reason_details(item, build, champion_name)
    phrases: list[str] = []
    for kind, enemies in grouped.items():
        template = _REASON_TEXT[language].get(kind, _REASON_TEXT[language]["direct"])
        if "{enemies}" in template:
            if not enemies:
                continue
            phrases.append(template.format(enemies=", ".join(enemies)))
        else:
            phrases.append(template)

    # Semantic de-duplication after localisation is deliberately kept as a
    # second safety net.  It protects against future source changes that may
    # create equivalent reason rows with slightly different internal tags.
    deduped: list[str] = []
    seen = set()
    for phrase in phrases:
        key = re.sub(r"[^a-zа-яё0-9]+", " ", phrase.casefold()).strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(phrase)
    if deduped:
        return "; ".join(deduped)

    return _REASON_TEXT[language]["core"]

def _append_unique_benefit(parts: list[tuple[str, str]], key: str, text: str) -> None:
    if text and key not in {k for k, _ in parts}:
        parts.append((key, text))

def _item_benefit_sentences(item: dict | None, lang: str = "ru") -> list[str]:
    """Human-readable item value, derived from stable semantics rather than raw prose.

    The updater may replace prices/stats/effect text, but it cannot overwrite
    these phrasing rules.  Source data is only used as evidence for mechanics;
    raw source descriptions are never shown to the user here.
    """
    item = item or {}
    language = "ru" if str(lang).lower().startswith("ru") else "en"
    name = str(item.get("name") or "")
    tags = engine.tags_for(name)
    stats = _item_stats_list(item)
    effect = clean_item_effect(str(item.get("effect_en") or ""))
    low = effect.casefold()
    benefits: list[tuple[str, str]] = []

    if language == "ru":
        if "anti_heal" in tags or "grievous wounds" in low:
            _append_unique_benefit(benefits, "anti_heal", "Снижает эффективность лечения и восстановления здоровья противников.")
        if "anti_shield" in tags:
            _append_unique_benefit(benefits, "anti_shield", "Помогает быстрее снимать и ослаблять вражеские щиты.")
        if "anti_crit" in tags:
            _append_unique_benefit(benefits, "anti_crit", "Снижает эффективность критических атак противника.")
        if "anti_attack_speed" in tags or "anti_auto" in tags:
            _append_unique_benefit(benefits, "anti_auto", "Снижает давление от частых базовых атак противников.")
        if "anti_magic" in tags:
            _append_unique_benefit(benefits, "anti_magic", "Повышает выживаемость против магического урона.")
        if "anti_physical" in tags:
            _append_unique_benefit(benefits, "anti_physical", "Повышает выживаемость против физического урона.")
        if "anti_burst" in tags:
            _append_unique_benefit(benefits, "anti_burst", "Помогает пережить резкий взрывной урон.")
        if "anti_cc" in tags:
            _append_unique_benefit(benefits, "anti_cc", "Помогает против контроля и ограничений передвижения.")
        if "anti_tank" in tags or "anti_armor" in tags:
            _append_unique_benefit(benefits, "anti_tank", "Помогает быстрее разбирать прочные цели и пробивать броню.")
        if "anti_magic_resist" in tags:
            _append_unique_benefit(benefits, "anti_magic_resist", "Помогает пробивать сопротивление магии.")

        if "slow" in low and "slow resist" not in low:
            _append_unique_benefit(benefits, "slow", "Помогает замедлять противников.")
        if "basic attack" in low or "basic attacks" in low:
            if "magic damage" in low:
                _append_unique_benefit(benefits, "onhit", "Усиливает базовые атаки дополнительным магическим уроном.")
            elif "physical damage" in low:
                _append_unique_benefit(benefits, "onhit", "Усиливает базовые атаки дополнительным физическим уроном.")
            elif "adaptive damage" in low:
                _append_unique_benefit(benefits, "onhit", "Усиливает базовые атаки дополнительным адаптивным уроном.")
        if "shield" in low and "anti_shield" not in tags:
            _append_unique_benefit(benefits, "self_shield", "Даёт дополнительную защиту за счёт щита.")
        if any(x in low for x in ("restore health", "restoring health", "heals you", "heal yourself", "heal for")):
            _append_unique_benefit(benefits, "self_heal", "Помогает восстанавливать здоровье в бою.")

        if _has_stat(stats, "ability power", " ap"):
            _append_unique_benefit(benefits, "ap", "Усиливает урон и эффективность умений.")
        if _has_stat(stats, "attack damage", " ad"):
            _append_unique_benefit(benefits, "ad", "Усиливает физический урон.")
        if _has_stat(stats, "attack speed", "% as", "+as"):
            _append_unique_benefit(benefits, "as", "Ускоряет базовые атаки.")
        if _has_stat(stats, "ability haste"):
            _append_unique_benefit(benefits, "haste", "Позволяет чаще использовать умения.")
        if _has_stat(stats, "armor") and "anti_physical" not in tags:
            _append_unique_benefit(benefits, "armor", "Повышает защиту от физического урона.")
        if _has_stat(stats, "magic resist", "mr") and "anti_magic" not in tags:
            _append_unique_benefit(benefits, "mr", "Повышает защиту от магического урона.")
        if _has_stat(stats, "hp", "health"):
            _append_unique_benefit(benefits, "hp", "Увеличивает запас здоровья.")
        if _has_stat(stats, "move", "movement", " ms"):
            _append_unique_benefit(benefits, "ms", "Повышает мобильность.")
        if _has_stat(stats, "mana"):
            _append_unique_benefit(benefits, "mana", "Увеличивает запас или восстановление маны.")
        if _has_stat(stats, "crit"):
            _append_unique_benefit(benefits, "crit", "Усиливает критические атаки.")
    else:
        if "anti_heal" in tags or "grievous wounds" in low:
            _append_unique_benefit(benefits, "anti_heal", "Reduces enemy healing and health regeneration.")
        if "anti_shield" in tags:
            _append_unique_benefit(benefits, "anti_shield", "Helps break and reduce enemy shields.")
        if "anti_crit" in tags:
            _append_unique_benefit(benefits, "anti_crit", "Reduces the effectiveness of enemy critical strikes.")
        if "anti_attack_speed" in tags or "anti_auto" in tags:
            _append_unique_benefit(benefits, "anti_auto", "Reduces pressure from repeated basic attacks.")
        if "anti_magic" in tags:
            _append_unique_benefit(benefits, "anti_magic", "Improves survivability against magic damage.")
        if "anti_physical" in tags:
            _append_unique_benefit(benefits, "anti_physical", "Improves survivability against physical damage.")
        if "anti_burst" in tags:
            _append_unique_benefit(benefits, "anti_burst", "Helps survive sudden burst damage.")
        if "anti_cc" in tags:
            _append_unique_benefit(benefits, "anti_cc", "Helps against crowd control and movement restrictions.")
        if "anti_tank" in tags or "anti_armor" in tags:
            _append_unique_benefit(benefits, "anti_tank", "Helps deal with durable targets and high Armor.")
        if "anti_magic_resist" in tags:
            _append_unique_benefit(benefits, "anti_magic_resist", "Helps penetrate Magic Resist.")

        if "slow" in low and "slow resist" not in low:
            _append_unique_benefit(benefits, "slow", "Helps slow enemies.")
        if "basic attack" in low or "basic attacks" in low:
            if "magic damage" in low:
                _append_unique_benefit(benefits, "onhit", "Adds extra magic damage to basic attacks.")
            elif "physical damage" in low:
                _append_unique_benefit(benefits, "onhit", "Adds extra physical damage to basic attacks.")
            elif "adaptive damage" in low:
                _append_unique_benefit(benefits, "onhit", "Adds extra adaptive damage to basic attacks.")
        if "shield" in low and "anti_shield" not in tags:
            _append_unique_benefit(benefits, "self_shield", "Provides extra protection through a shield.")
        if any(x in low for x in ("restore health", "restoring health", "heals you", "heal yourself", "heal for")):
            _append_unique_benefit(benefits, "self_heal", "Helps restore Health during fights.")

        if _has_stat(stats, "ability power", " ap"):
            _append_unique_benefit(benefits, "ap", "Improves ability damage and effectiveness.")
        if _has_stat(stats, "attack damage", " ad"):
            _append_unique_benefit(benefits, "ad", "Increases physical damage.")
        if _has_stat(stats, "attack speed", "% as", "+as"):
            _append_unique_benefit(benefits, "as", "Speeds up basic attacks.")
        if _has_stat(stats, "ability haste"):
            _append_unique_benefit(benefits, "haste", "Lets you use abilities more often.")
        if _has_stat(stats, "armor") and "anti_physical" not in tags:
            _append_unique_benefit(benefits, "armor", "Improves protection against physical damage.")
        if _has_stat(stats, "magic resist", "mr") and "anti_magic" not in tags:
            _append_unique_benefit(benefits, "mr", "Improves protection against magic damage.")
        if _has_stat(stats, "hp", "health"):
            _append_unique_benefit(benefits, "hp", "Increases Health.")
        if _has_stat(stats, "move", "movement", " ms"):
            _append_unique_benefit(benefits, "ms", "Improves mobility.")
        if _has_stat(stats, "mana"):
            _append_unique_benefit(benefits, "mana", "Increases Mana or Mana regeneration.")
        if _has_stat(stats, "crit"):
            _append_unique_benefit(benefits, "crit", "Improves critical strikes.")

    if not benefits:
        return [
            "Усиливает ключевые характеристики героя и поддерживает его основную сборку."
            if language == "ru" else
            "Improves the hero's key stats and supports the standard build."
        ]
    return [text for _key, text in benefits[:3]]

def contextual_item_explanation(item: dict | None, build: dict | None, lang: str, champion_name) -> tuple[str, list[str]]:
    """Return (why-this-item, what-it-provides) from one shared generator."""
    item = item or {}
    item_name = str(item.get("name") or "")
    language = "ru" if str(lang).lower().startswith("ru") else "en"
    why = _localized_build_item_reason(item_name, build or {}, language, champion_name)
    benefits = _item_benefit_sentences(item, language)
    return why, benefits
