from __future__ import annotations

import re
import unicodedata


def normalize_search(text: str) -> str:
    """Normalize EN/RU champion queries for tolerant local matching."""
    value = unicodedata.normalize("NFKC", text or "").casefold().strip()
    value = value.replace("’", "'").replace("`", "'")
    value = value.replace("ё", "е").replace("э", "е")
    value = re.sub(r"[^a-zа-я0-9]+", "", value)
    return value


def has_cyrillic(text: str) -> bool:
    """True when a supposedly Russian value contains at least one Cyrillic letter."""
    return bool(re.search(r"[А-Яа-яЁё]", str(text or "")))


# Stable local fallbacks for heroes that can appear in a catalog before Rone has
# published the corresponding numeric/live record.  Keep English IDs canonical;
# this table is display/search localization only.
BUILTIN_HERO_NAMES_RU: dict[str, str] = {
    "dori": "Дори",
}


# Built-in EN -> RU equipment names based on the Russian MLBB Wiki naming table.
# This is deliberately local: an endpoint answering to lang=ru is not considered
# translated unless the returned text actually contains Cyrillic.
RUSSIAN_ITEM_NAMES: dict[str, str] = {
    "Berserker's Fury": "Ярость берсерка",
    "Blade of Despair": "Клинок отчаяния",
    "Blade of the Heptaseas": "Клинок семи морей",
    "Corrosion Scythe": "Коса коррозии",
    "Demon Hunter Sword": "Меч охотника на демонов",
    "Endless Battle": "Бесконечная битва",
    "Golden Staff": "Золотой посох",
    "Great Dragon Spear": "Копьё великого дракона",
    "Haas' Claws": "Когти Хааса",
    "Hunter Strike": "Удар охотника",
    "Malefic Gun": "Зловещее ружьё",
    "Malefic Roar": "Зловещий рёв",
    "Rose Gold Meteor": "Метеор из розового золота",
    "Sea Halberd": "Морская алебарда",
    "War Axe": "Топор войны",
    "Wind of Nature": "Ветер природы",
    "Windtalker": "Говорящий с ветром",
    "Sky Piercer": "Небесный пронзатель",
    "Winter Crown": "Зимняя корона",
    "Fleeting Time": "Мимолётное время",
    "Antique Cuirass": "Древняя кираса",
    "Athena's Shield": "Щит Афины",
    "Blade Armor": "Броня с лезвиями",
    "Brute Force Breastplate": "Кираса грубой силы",
    "Chastise Pauldron": "Карающий наплечник",
    "Cursed Helmet": "Проклятый шлем",
    "Dominance Ice": "Господство льда",
    "Guardian Helmet": "Шлем стража",
    "Immortality": "Бессмертие",
    "Oracle": "Оракул",
    "Queen's Wings": "Крылья королевы",
    "Radiant Armor": "Сияющая броня",
    "Thunder Belt": "Громовой пояс",
    "Blood Wings": "Кровавые крылья",
    "Clock of Destiny": "Часы судьбы",
    "Concentrated Energy": "Концентрированная энергия",
    "Divine Glaive": "Божественная глефа",
    "Enchanted Talisman": "Зачарованный талисман",
    "Feather of Heaven": "Небесное перо",
    "Flask of the Oasis": "Фляга оазиса",
    "Flower of Hope": "Цветок надежды",
    "Genius Wand": "Гениальный жезл",
    "Glowing Wand": "Светящийся жезл",
    "Holy Crystal": "Священный кристалл",
    "Ice Queen Wand": "Жезл ледяной королевы",
    "Lantern of Hope": "Фонарь надежды",
    "Lightning Truncheon": "Жезл молний",
    "Starlium Scythe": "Коса Старлиума",
    "Wishing Lantern": "Фонарь желаний",
    "Arcane Boots": "Сапоги заклинателя",
    "Demon Shoes": "Демонические ботинки",
    "Magic Shoes": "Магические ботинки",
    "Rapid Boots": "Сапоги стремительности",
    "Swift Boots": "Сапоги скорости",
    "Tough Boots": "Прочные сапоги",
    "Warrior Boots": "Сапоги воина",
}

_RUSSIAN_ITEM_NAMES_BY_NORM = {
    normalize_search(name): value for name, value in RUSSIAN_ITEM_NAMES.items()
}


def russian_item_name(english_name: str) -> str:
    """Return a stable Russian display name for an English canonical item name."""
    return _RUSSIAN_ITEM_NAMES_BY_NORM.get(normalize_search(english_name), "")


def russian_hero_name(hero_id: str, english_name: str = "") -> str:
    value = BUILTIN_HERO_NAMES_RU.get(str(hero_id or "").casefold(), "")
    if value:
        return value
    return BUILTIN_HERO_NAMES_RU.get(normalize_search(english_name), "")


# Hand-written aliases are also used by local search.  Dori is present here so
# the RU name works immediately even while the hero still comes only from the
# MLBBDex reserve catalog.
COMMON_CHAMPION_ALIASES: dict[str, tuple[str, ...]] = {
    "dori": ("Дори",),
}
