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


# Common spellings/transliterations which are not reliably solved by punctuation removal.
CHAMPION_RU_OVERRIDES: dict[str, str] = {
    "Norra": "Норра",
    "Nunu": "Нуну и Виллумп",
}


def champion_name_ru(champion_id: str, fallback_name: str = "") -> str:
    """Return a stable RU display name for identities that upstream RU feeds may miss."""
    return CHAMPION_RU_OVERRIDES.get(str(champion_id or ""), "")


COMMON_CHAMPION_ALIASES: dict[str, tuple[str, ...]] = {
    "Sett": ("Сет", "Сэт", "Сетт", "Сэтт"),
    "Kaisa": ("Кайса", "Кай'Са", "Кай Са", "Кай-Са"),
    "Khazix": ("Казикс", "Ка'Зикс", "Ка Зикс", "Кха Зикс"),
    "DrMundo": ("Доктор Мундо", "Др Мундо", "Мундо"),
    "JarvanIV": ("Джарван 4", "Джарван IV", "Джарван Четвертый"),
    "Nunu": (
        "Nunu & Willump", "Nunu And Willump", "Nunu and Willump",
        "Нуну", "Нуну и Виллумп", "Нуну и Вилламп",
    ),
    "Wukong": ("Вуконг", "Укун"),
}
