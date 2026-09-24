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
COMMON_CHAMPION_ALIASES: dict[str, tuple[str, ...]] = {
    "Sett": ("Сет", "Сэт", "Сетт", "Сэтт"),
    "Kaisa": ("Кайса", "Кай'Са", "Кай Са", "Кай-Са"),
    "Khazix": ("Казикс", "Ка'Зикс", "Ка Зикс", "Кха Зикс"),
    "DrMundo": ("Доктор Мундо", "Др Мундо", "Мундо"),
    "JarvanIV": ("Джарван 4", "Джарван IV", "Джарван Четвертый"),
    "Nunu": ("Нуну", "Нуну и Виллумп", "Нуну и Вилламп"),
    "Wukong": ("Вуконг", "Укун"),
}
