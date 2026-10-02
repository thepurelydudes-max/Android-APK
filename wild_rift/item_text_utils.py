"""Small runtime-only item text cleaners used by adaptive descriptions.

These helpers are deliberately independent from sources.py. WRCA 3.9+ no longer
ships web scrapers in the APK: all data collection happens in GitHub packages.
"""
from __future__ import annotations

import re
from typing import Iterable


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


_ITEM_STAT_TERMS = (
    "Ability Haste", "Attack Damage", "Ability Power", "Attack Speed",
    "Armor Penetration", "Magic Penetration", "Movement Speed",
    "Armor Pen", "Magic Pen", "Move Speed", "Magic Resist",
    "Physical Vamp", "Magic Vamp", "Omni Vamp", "Omnivamp",
    "Health Regen", "Mana Regen", "Life Steal", "Lifesteal",
    "Tenacity", "Critical Strike Chance", "Crit Chance", "Crit",
    "Health", "Mana", "Armor", "AD", "AP", "AS", "HP", "MR", "MS",
)
_ITEM_STAT_RE = re.compile(
    r"\+\s*\d+(?:\.\d+)?\s*%?\s*(?:"
    + "|".join(re.escape(x) for x in _ITEM_STAT_TERMS)
    + r")(?![A-Za-z])",
    flags=re.I,
)

_ITEM_EFFECT_STOP = re.compile(
    r"\b(?:Recipe|Builds Into|Similar Items|Build Trends|Detail page|Gold Eff)\b",
    flags=re.I,
)


def clean_item_stats(lines: Iterable[str] | str) -> list[str]:
    """Keep only real numeric item stats from stored item text."""
    if isinstance(lines, str):
        raw_lines = [lines]
    else:
        raw_lines = [str(x or "") for x in (lines or [])]
    out: list[str] = []
    seen: set[str] = set()
    effect_words = re.compile(
        r"\b(?:unique|gain|gains|deal|deals|damage|attack(?:s|ing)?|when|after|target|enemy|champion|cooldown)\b",
        flags=re.I,
    )
    for raw in raw_lines:
        line = _clean(raw)
        if not line:
            continue
        if effect_words.search(line) and not line.lstrip().startswith("+"):
            continue
        for match in _ITEM_STAT_RE.finditer(line):
            value = _clean(match.group(0))
            value = re.sub(r"^\+\s+", "+", value)
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                out.append(value)
    return out


def clean_item_effect(lines: Iterable[str] | str) -> str:
    """Normalize one passive/active effect without page chrome or item tags."""
    if isinstance(lines, str):
        raw_lines = [lines]
    else:
        raw_lines = [str(x or "") for x in (lines or [])]
    kept: list[str] = []
    for raw in raw_lines:
        line = _clean(raw)
        if not line:
            continue
        folded = line.casefold()
        # Recipe/build-navigation marks the end of the actual effect section.
        # Stop immediately instead of skipping the label and accidentally
        # appending page chrome that follows it.
        if folded == "recipe":
            break
        if folded in {
            "effect", "stats", "physical", "magic",
            "upgraded", "adaptive", "on-hit",
        }:
            continue
        stop = _ITEM_EFFECT_STOP.search(line)
        if stop:
            line = _clean(line[:stop.start()])
        if line:
            kept.append(line)
        if stop:
            break
    text = _clean(" ".join(kept))
    if len(text) > 1800:
        text = text[:1800].rsplit(" ", 1)[0].rstrip() + "…"
    return text
