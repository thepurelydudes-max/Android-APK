from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from pathlib import Path
from dataclasses import dataclass
from typing import Iterable

import requests

from localization import has_cyrillic

HEADERS = {
    "User-Agent": "MobileLegendsCounterAssistant/1.0 (+personal offline tool; respectful updater)",
    "Accept-Language": "en-US,en;q=0.9",
}


@dataclass
class Net:
    timeout: int = 25
    delay: float = 0.18

    def __post_init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def get(self, url: str, headers: dict | None = None, allow_not_modified: bool = False) -> requests.Response:
        response = self.s.get(url, timeout=self.timeout, headers=headers or None)
        if not (allow_not_modified and response.status_code == 304):
            response.raise_for_status()
        time.sleep(self.delay)
        return response


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def clean_item_name(value: str) -> str:
    """Normalize whitespace/punctuation in item names from community feeds."""
    value = clean(value)
    return value.replace("â", "’").replace("â€™", "’").replace("`", "'")


def slugish(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.casefold().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", "", value)


def _pct(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric * 100 if 0 <= numeric <= 1 else numeric
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group()) if match else None


_ITEM_STAT_TERMS = (
    "Physical Attack", "Magic Power", "Physical Defense", "Magic Defense",
    "Cooldown Reduction", "Hybrid Defense", "Hybrid Lifesteal", "Spell Vamp",
    "Crit Damage Reduction", "Critical Chance", "HP Regen",
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


def clean_item_stats(lines: Iterable[str] | str) -> list[str]:
    """Keep base numeric stats and discard prose/passive fragments."""
    raw_lines = [lines] if isinstance(lines, str) else [str(x or "") for x in (lines or [])]
    out: list[str] = []
    seen: set[str] = set()
    effect_words = re.compile(
        r"\b(?:unique|gain|gains|deal|deals|damage|attack(?:s|ing)?|when|after|target|enemy|hero|cooldown)\b",
        flags=re.I,
    )
    for raw in raw_lines:
        line = clean(str(raw or ""))
        if not line:
            continue
        if effect_words.search(line) and not line.lstrip().startswith("+"):
            continue
        for match in _ITEM_STAT_RE.finditer(line):
            value = re.sub(r"^\+\s+", "+", clean(match.group(0)))
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                out.append(value)
    return out


_ITEM_EFFECT_STOP = re.compile(
    r"\b(?:Recipe|Builds Into|Similar Items|Build Trends|Detail page|Gold Eff)\b",
    flags=re.I,
)


def clean_item_effect(lines: Iterable[str] | str) -> str:
    """Normalize passive/active effect prose returned by item data sources."""
    raw_lines = [lines] if isinstance(lines, str) else [str(x or "") for x in (lines or [])]
    kept: list[str] = []
    for raw in raw_lines:
        raw = re.sub(r"<br\s*/?>", " ", str(raw or ""), flags=re.I)
        raw = re.sub(r"<[^>]+>", "", raw)
        line = clean(html.unescape(raw))
        if not line:
            continue
        folded = line.casefold()
        if folded in {"effect", "stats", "recipe", "physical", "magic", "upgraded", "adaptive", "on-hit"}:
            continue
        stop = _ITEM_EFFECT_STOP.search(line)
        if stop:
            line = clean(line[:stop.start()])
        if line:
            kept.append(line)
        if stop:
            break
    text = clean(" ".join(kept))
    if len(text) > 1800:
        text = text[:1800].rsplit(" ", 1)[0].rstrip() + "…"
    return text



def load_ru_localization(path: str | Path) -> dict:
    """Load the persistent RU localization sidecar without touching live data."""
    target = Path(path)
    if not target.is_file():
        return {"heroes": {}, "items": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"heroes": {}, "items": {}}
    if not isinstance(payload, dict):
        return {"heroes": {}, "items": {}}
    heroes = payload.get("heroes") if isinstance(payload.get("heroes"), dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
    return {"heroes": heroes, "items": items}


def save_ru_localization(path: str | Path, payload: dict) -> None:
    """Atomically persist localized names/effects beside the SQLite database."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        "heroes": payload.get("heroes", {}) if isinstance(payload, dict) else {},
        "items": payload.get("items", {}) if isinstance(payload, dict) else {},
    }
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(target)


def apply_ru_localization(heroes: list[dict], items: list[dict], localization: dict) -> None:
    """Apply sidecar localization in-place while keeping English keys canonical."""
    hero_map = localization.get("heroes", {}) if isinstance(localization, dict) else {}
    item_map = localization.get("items", {}) if isinstance(localization, dict) else {}
    if not isinstance(hero_map, dict):
        hero_map = {}
    if not isinstance(item_map, dict):
        item_map = {}

    for hero in heroes:
        key = str(hero.get("id") or "")
        entry = hero_map.get(key, {})
        candidate = str(entry.get("name_ru") or "").strip() if isinstance(entry, dict) else ""
        if not str(hero.get("name_ru") or "").strip() and has_cyrillic(candidate):
            hero["name_ru"] = candidate

    by_slug = {slugish(str(key)): value for key, value in item_map.items() if str(key).strip()}
    for item in items:
        entry = item_map.get(str(item.get("name") or ""))
        if not isinstance(entry, dict):
            entry = by_slug.get(slugish(str(item.get("name") or "")), {})
        if not isinstance(entry, dict):
            continue
        candidate_name = str(entry.get("name_ru") or "").strip()
        candidate_effect = clean_item_effect(str(entry.get("effect_ru") or ""))
        if not str(item.get("name_ru") or "").strip() and has_cyrillic(candidate_name):
            item["name_ru"] = candidate_name
        if not str(item.get("effect_ru") or "").strip() and has_cyrillic(candidate_effect):
            item["effect_ru"] = candidate_effect

# ---------------------------------------------------------------------------
# Mobile Legends: Bang Bang data adapters
# ---------------------------------------------------------------------------
MLBBDEX_BASE = "https://mlbbdex.com/api/v1"
RONE_BASE = "https://arena.rone.dev/api"
INSIGHT_RAW_BASE = "https://raw.githubusercontent.com/Rafael-VH/Insight-Data-MLBB/main"

MLBBDEX_HEROES = f"{MLBBDEX_BASE}/heroes"
MLBBDEX_ITEMS = f"{MLBBDEX_BASE}/items"
MLBBDEX_PATCHES = f"{MLBBDEX_BASE}/patches"
MLBBDEX_RANKINGS = f"{MLBBDEX_BASE}/rankings"
RONE_EQUIPMENT_EXPANDED = f"{RONE_BASE}/academy/equipment/expanded"
RONE_VERSION = f"{RONE_BASE}/academy/meta/version"
INSIGHT_COUNTERS = f"{INSIGHT_RAW_BASE}/heroes/hero_counters.json"
INSIGHT_BUILDS = f"{INSIGHT_RAW_BASE}/guides/guide_builds.json"


def _json_data(payload):
    if isinstance(payload, dict):
        return payload.get("data", payload)
    return payload


def parse_mlbbdex_heroes(payload: dict) -> list[dict]:
    rows = _json_data(payload) or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slug = clean(str(row.get("slug") or ""))
        name = clean(str(row.get("name") or ""))
        if not slug or not name:
            continue
        raw_roles = row.get("roles") if row.get("roles") is not None else row.get("role")
        raw_lanes = row.get("lanes") if row.get("lanes") is not None else row.get("lane")
        raw_specialties = row.get("specialties") if row.get("specialties") is not None else row.get("specialty")
        if isinstance(raw_roles, str):
            raw_roles = [raw_roles]
        if isinstance(raw_lanes, str):
            raw_lanes = [raw_lanes]
        if isinstance(raw_specialties, str):
            raw_specialties = [raw_specialties]
        roles = [clean(str(x)) for x in (raw_roles or []) if clean(str(x))]
        lanes = [clean(str(x)).casefold() for x in (raw_lanes or []) if clean(str(x))]
        specialties = [clean(str(x)) for x in (raw_specialties or []) if clean(str(x))]
        out.append({
            "id": slug,
            "slug": slug,
            "name": name,
            "roles": roles,
            "lanes": lanes,
            "specialties": specialties,
            "damage_type": clean(str(row.get("damageType") or row.get("damage_type") or "")),
            "icon_url": clean(str(row.get("icon_url") or row.get("icon") or "")),
        })
    return out


def _split_bonus_stats(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for part in value:
            out.extend(_split_bonus_stats(part))
        return list(dict.fromkeys(out))
    if isinstance(value, dict):
        out: list[str] = []
        for key, raw in value.items():
            if raw in (None, ""):
                continue
            if isinstance(raw, (int, float)):
                out.append(f"{raw} {key}")
            else:
                out.extend(_split_bonus_stats(raw))
        return list(dict.fromkeys(out))
    text = clean(str(value or ""))
    if not text:
        return []
    return [clean(x) for x in re.split(r",\s*|\n+", text) if clean(x)]


def _recipe_component_name(value) -> str:
    if isinstance(value, str):
        return clean_item_name(value)
    if isinstance(value, dict):
        for key in ("name", "item_name", "item", "component"):
            raw = value.get(key)
            if isinstance(raw, str) and clean_item_name(raw):
                return clean_item_name(raw)
    return ""


def parse_mlbbdex_items(payload: dict) -> list[dict]:
    """Normalize MLBBDex shop data and keep final/terminal equipment only.

    MLBBDex exposes components and completed items in one list without an
    explicit tier.  A component is identifiable because its name is referenced
    by another item's recipe.  Completed equipment is therefore a terminal
    node in the recipe graph.  Removed items and temporary potions are excluded.
    """
    rows = _json_data(payload) or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    rows = [r for r in rows if isinstance(r, dict)]
    components = {
        _recipe_component_name(component).casefold()
        for row in rows
        for component in (row.get("recipe") or [])
        if _recipe_component_name(component)
    }
    out: list[dict] = []
    for row in rows:
        name = clean_item_name(str(row.get("name") or ""))
        if not name or name.casefold() in components:
            continue
        if clean(str(row.get("bestFor") or "")).casefold() == "removed":
            continue
        if "potion" in name.casefold():
            continue
        slug = clean(str(row.get("slug") or ""))
        stats = _split_bonus_stats(row.get("bonus"))
        unique = clean(str(row.get("unique") or ""))
        if unique:
            stats.append(unique)
        effect_parts = [
            clean(str(row.get("summary") or "")),
            clean(str(row.get("passive") or "")),
            clean(str(row.get("active") or "")),
        ]
        effect_en = " ".join(x for x in effect_parts if x)
        out.append({
            "id": slug or slugish(name),
            "slug": slug,
            "name": name,
            "category": clean(str(row.get("category") or "")),
            "price": int(row.get("price") or 0),
            "stats": list(dict.fromkeys(stats)),
            "effect_en": effect_en,
            "recipe": [_recipe_component_name(x) for x in (row.get("recipe") or []) if _recipe_component_name(x)],
            "tier": "Upgraded",
            "icon_url": clean(str(row.get("icon_url") or row.get("icon") or "")),
        })
    return out


def parse_mlbbdex_rankings(payload: dict) -> list[dict]:
    data = _json_data(payload) or []
    measured_at = ""
    if isinstance(data, dict):
        measured_at = clean(str(data.get("measuredAt") or data.get("measured_at") or ""))
        rows = data.get("heroes") or data.get("rankings") or data.get("records") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
    else:
        rows = data

    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        name = clean(str(row.get("name") or row.get("hero") or row.get("hero_name") or ""))
        slug = clean(str(row.get("slug") or row.get("hero_slug") or ""))
        if not (name or slug):
            continue
        tier = clean(str(row.get("tier") or "")).upper()
        if tier not in {"S+", "S", "A", "B", "C", "D"}:
            tier = ""
        out.append({
            "id": slug,
            "name": name,
            "win_rate": _pct(row.get("winRate") if "winRate" in row else row.get("win_rate")),
            "pick_rate": _pct(row.get("pickRate") if "pickRate" in row else row.get("pick_rate")),
            "ban_rate": _pct(row.get("banRate") if "banRate" in row else row.get("ban_rate")),
            "tier": tier,
            "date": clean(str(row.get("date") or row.get("recordedAt") or row.get("updated_at") or measured_at)),
        })
    return out



def parse_rone_counter_payload(payload: dict) -> list[tuple[str, str, str, float]]:
    """Return matchup edges oriented as candidate -> target.

    Rone's counter response groups stronger counters under ``sub_hero`` and the
    opposite side under ``sub_hero_last``.  The application expects a positive
    score when the first hero is a good answer into the second hero.
    """
    data = _json_data(payload) or {}
    records = data.get("records", []) if isinstance(data, dict) else []
    edges: dict[tuple[str, str, str], float] = {}
    for record in records:
        row = (record or {}).get("data") or {}
        target = row.get("main_heroid") or row.get("heroid")
        if target is None:
            continue
        target = str(target)
        for key, sign in (("sub_hero", 1.0), ("sub_hero_last", -1.0)):
            for sub in row.get(key) or []:
                cid = sub.get("heroid") if isinstance(sub, dict) else None
                if cid is None or str(cid) == target:
                    continue
                delta = abs(float(sub.get("increase_win_rate") or 0.0)) if isinstance(sub, dict) else 0.0
                magnitude = max(0.20, min(1.50, delta * 10.0))
                edges[(str(cid), target, "")] = sign * magnitude
                edges[(target, str(cid), "")] = -sign * magnitude
    return [(a, b, role, score) for (a, b, role), score in edges.items()]


def parse_rone_equipment(payload: dict) -> tuple[list[dict], dict[int, str]]:
    data = _json_data(payload) or {}
    records = data.get("records", []) if isinstance(data, dict) else []
    out: list[dict] = []
    by_id: dict[int, str] = {}
    for record in records:
        row = (record or {}).get("data") or {}
        eid = row.get("equipid")
        name = clean_item_name(str(row.get("equipname") or ""))
        if eid is None or not name:
            continue
        try:
            by_id[int(eid)] = name
        except (TypeError, ValueError):
            pass
        stats = clean_item_stats(
            re.split(r"<br\s*/?>|\n+", str(row.get("equiptips") or ""), flags=re.I)
        )
        effect = clean_item_effect(str(row.get("equipskilldesc") or ""))
        out.append({
            "id": str(eid),
            "name": name,
            "category": clean(str(row.get("equiptypename") or "")),
            "price": 0,
            "stats": stats,
            "effect_en": effect,
            "tier": "Upgraded",
            "icon_url": clean(str(row.get("equipicon") or "")),
        })
    return out, by_id


def parse_rone_build_payload(payload: dict, equipment_by_id: dict[int, str]) -> list[tuple[str, str, str, int]]:
    data = _json_data(payload) or {}
    records = data.get("records", []) if isinstance(data, dict) else []
    builds: list[tuple[float, str, list]] = []
    for record in records:
        row = (record or {}).get("data") or {}
        champion_id = row.get("heroid") or row.get("hero_id")
        if champion_id is None:
            continue
        for build in row.get("build") or []:
            if not isinstance(build, dict):
                continue
            equip_ids = build.get("equipid") or build.get("equipment_ids") or []
            if not isinstance(equip_ids, list):
                continue
            pick = float(build.get("build_pick_rate") or 0.0)
            win = float(build.get("build_win_rate") or 0.0)
            builds.append((pick + win * 0.01, str(champion_id), equip_ids))
    if not builds:
        return []
    builds.sort(key=lambda x: x[0], reverse=True)
    _, champion_id, equip_ids = builds[0]
    out: list[tuple[str, str, str, int]] = []
    seen: set[str] = set()
    priority = 1
    for raw_id in equip_ids:
        try:
            name = equipment_by_id.get(int(raw_id), "")
        except (TypeError, ValueError):
            name = ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((champion_id, name, "", priority))
        priority += 1
    return out


def fetch_mlbbdex_heroes(net: Net) -> list[dict]:
    return parse_mlbbdex_heroes(net.get(MLBBDEX_HEROES).json())


def fetch_mlbbdex_items(net: Net) -> list[dict]:
    return parse_mlbbdex_items(net.get(MLBBDEX_ITEMS).json())


def fetch_mlbbdex_rankings(net: Net) -> list[dict]:
    return parse_mlbbdex_rankings(net.get(MLBBDEX_RANKINGS).json())


def fetch_mlbb_patch_info(net: Net) -> tuple[str, str]:
    try:
        root = net.get(MLBBDEX_PATCHES).json()
        rows = _json_data(root) or []
        if isinstance(rows, list) and rows:
            row = rows[0] if isinstance(rows[0], dict) else {"version": rows[0]}
            version = clean(str(row.get("version") or row.get("patch") or row.get("name") or ""))
            date = clean(str(row.get("date") or row.get("release") or row.get("releasedAt") or ""))
            if version:
                return version, date
    except Exception:
        pass
    root = net.get(RONE_VERSION, headers=None).json()
    data = _json_data(root) or {}
    records = data.get("records", []) if isinstance(data, dict) else []
    if records:
        row = (records[0] or {}).get("data") or {}
        return clean(str(row.get("game_version") or "")), ""
    return "", ""

# Efficient current-data endpoints used by the MLBB updater.
RONE_PUBLIC_HEROES = f"{RONE_BASE}/heroes"
RONE_HERO_RANK = f"{RONE_BASE}/heroes/rank"
RONE_ACADEMY_RECOMMENDED = f"{RONE_BASE}/academy/recommended"


def _id_list(value) -> list[str]:
    if isinstance(value, dict):
        value = value.get("target_hero_id") or value.get("hero_ids") or value.get("ids") or []
    if not isinstance(value, list):
        return []
    return [str(x) for x in value if x is not None and str(x).strip()]


def _nested_titles(values, *keys: str) -> list[str]:
    out: list[str] = []
    for value in values or []:
        if isinstance(value, str):
            title = clean(value)
        elif isinstance(value, dict):
            data = value.get("data") if isinstance(value.get("data"), dict) else value
            title = ""
            for key in keys:
                if data.get(key):
                    title = clean(str(data.get(key)))
                    break
        else:
            title = ""
        if title and title not in out:
            out.append(title)
    return out


def parse_rone_public_heroes(payload: dict) -> list[dict]:
    """Parse the compact Rone hero list including roles, lanes and relations."""
    data = _json_data(payload) or {}
    records = data.get("records", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for record in records:
        row = (record or {}).get("data") or {}
        hero = ((row.get("hero") or {}).get("data") or {})
        hero_id = row.get("hero_id") or row.get("heroid") or hero.get("heroid")
        name = clean(str(hero.get("name") or row.get("hero_name") or ""))
        if hero_id is None or not name:
            continue
        relation = row.get("relation") or {}
        roles = _nested_titles(hero.get("sortid") or row.get("sortid") or [], "sort_title", "title", "name")
        lanes = [
            value.casefold().replace(" lane", "")
            for value in _nested_titles(hero.get("roadsort") or row.get("roadsort") or [], "road_sort_title", "title", "name")
        ]
        out.append({
            "id": str(hero_id),
            "name": name,
            "icon_url": clean(str(hero.get("head") or row.get("head") or "")),
            "roles": roles,
            "lanes": lanes,
            "strong": _id_list(relation.get("strong") or {}),
            "weak": _id_list(relation.get("weak") or {}),
            "assist": _id_list(relation.get("assist") or {}),
        })
    return out


def parse_rone_rank_payload(payload: dict) -> list[dict]:
    """Normalize the all-hero Rone rank endpoint to percent values."""
    data = _json_data(payload) or {}
    records = data.get("records", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for record in records:
        row = (record or {}).get("data") or {}
        cid = row.get("main_heroid") or row.get("heroid") or row.get("hero_id")
        if cid is None:
            continue
        win = row.get("main_hero_win_rate", row.get("win_rate"))
        pick = row.get("main_hero_appearance_rate", row.get("main_hero_pick_rate", row.get("pick_rate")))
        ban = row.get("main_hero_ban_rate", row.get("ban_rate"))
        out.append({
            "champion_id": str(cid),
            "win_rate": _pct(win),
            "pick_rate": _pct(pick),
            "ban_rate": _pct(ban),
            "date": clean(str(row.get("date") or row.get("record_date") or "")),
        })
    return out


def _recommended_payload_data(record: dict) -> dict:
    outer = (record or {}).get("data") or {}
    inner = outer.get("data") if isinstance(outer, dict) else None
    return inner if isinstance(inner, dict) else outer if isinstance(outer, dict) else {}


def parse_rone_recommended_payload(payload: dict, equipment_by_id: dict[int, str]) -> tuple[list[tuple[str, str, str, int]], list[tuple[str, str, str, float]]]:
    """Extract build pools and matchup hints from Academy recommended posts.

    This endpoint is a live supplement, not the only source. Multiple posts for
    one hero are de-duplicated while preserving the first practical six-item
    order. Counter rates are converted to a small positive candidate->target
    matchup score.
    """
    data = _json_data(payload) or {}
    records = data.get("records", []) if isinstance(data, dict) else []
    pool_rows: list[tuple[str, str, str, int]] = []
    counter_rows: list[tuple[str, str, str, float]] = []
    seen_pool: set[tuple[str, str]] = set()
    seen_edges: set[tuple[str, str]] = set()
    for record in records:
        row = _recommended_payload_data(record)
        hero = row.get("hero") or {}
        cid = hero.get("hero_id") or row.get("hero_id")
        if cid is None:
            continue
        cid = str(cid)
        priority = 1
        for build in row.get("equips") or []:
            ids = (build or {}).get("equip_ids") or [] if isinstance(build, dict) else []
            for raw_id in ids:
                try:
                    name = equipment_by_id.get(int(raw_id), "")
                except (TypeError, ValueError):
                    name = ""
                key = (cid, name)
                if not name or key in seen_pool:
                    continue
                seen_pool.add(key)
                pool_rows.append((cid, name, "", priority))
                priority += 1
        for counter in row.get("counters") or []:
            if not isinstance(counter, dict):
                continue
            counter_id = counter.get("counter_hero_id") or counter.get("hero_id")
            if counter_id is None or str(counter_id) == cid:
                continue
            edge_key = (str(counter_id), cid)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            try:
                rate = abs(float(counter.get("counter_rate") or 0.0))
            except (TypeError, ValueError):
                rate = 0.0
            magnitude = max(0.35, min(1.50, rate / 20.0 if rate else 0.60))
            counter_rows.append((str(counter_id), cid, "", magnitude))
            counter_rows.append((cid, str(counter_id), "", -magnitude))
    return pool_rows, counter_rows


def parse_insight_counters(payload: dict) -> list[tuple[str, str, str, float]]:
    """Parse both historical and current Insight hero_counters.json snapshots.

    The current repository stores one complete Rone response under each hero ID,
    while older snapshots used direct ``counters`` / ``countered_by`` arrays.
    """
    data = _json_data(payload) or {}
    if not isinstance(data, dict):
        return []
    edges: dict[tuple[str, str, str], float] = {}

    def put(a: str, b: str, score: float) -> None:
        key = (str(a), str(b), "")
        previous = edges.get(key)
        if previous is None or abs(score) > abs(previous):
            edges[key] = score

    def magnitude(entry: dict) -> float:
        raw = entry.get("advantage", entry.get("win_rate", entry.get("rate", 0.0)))
        try:
            value = abs(float(raw or 0.0))
        except (TypeError, ValueError):
            value = 0.0
        if value > 1.5:
            value /= 10.0
        return max(0.25, min(1.50, value or 0.60))

    for target_id, block in data.items():
        if not isinstance(block, dict):
            continue

        # Current Insight snapshot: each value is the original Rone payload.
        nested_data = block.get("data")
        if isinstance(nested_data, dict) and isinstance(nested_data.get("records"), list):
            for a, b, _role, score in parse_rone_counter_payload(block):
                put(a, b, score)
            continue

        # Backward-compatible support for older flattened snapshots.
        target = str(block.get("hero_id") or target_id)
        for entry in block.get("counters") or block.get("counter_heroes") or []:
            if not isinstance(entry, dict):
                continue
            counter = entry.get("hero_id") or entry.get("heroid")
            if counter is None or str(counter) == target:
                continue
            mag = magnitude(entry)
            put(str(counter), target, mag)
            put(target, str(counter), -mag)
        for entry in block.get("countered_by") or block.get("strong_against") or []:
            if not isinstance(entry, dict):
                continue
            victim = entry.get("hero_id") or entry.get("heroid")
            if victim is None or str(victim) == target:
                continue
            mag = magnitude(entry)
            put(target, str(victim), mag)
            put(str(victim), target, -mag)
    return [(a, b, role, score) for (a, b, role), score in edges.items()]


def _equipment_name(value, equipment_by_id: dict[int, str]) -> str:
    if isinstance(value, str):
        return clean_item_name(value)
    if isinstance(value, (int, float)):
        try:
            return equipment_by_id.get(int(value), "")
        except (TypeError, ValueError):
            return ""
    if not isinstance(value, dict):
        return ""
    for key in ("name", "equip_name", "equipname", "item_name"):
        if value.get(key):
            return clean_item_name(str(value.get(key)))
    raw_id = value.get("equip_id", value.get("equipid", value.get("id")))
    try:
        return equipment_by_id.get(int(raw_id), "") if raw_id is not None else ""
    except (TypeError, ValueError):
        return ""


def parse_insight_builds(payload: dict, equipment_by_id: dict[int, str] | None = None) -> list[tuple[str, str, str, int]]:
    """Parse both historical and current Insight guide_builds.json snapshots."""
    equipment_by_id = equipment_by_id or {}
    data = _json_data(payload) or {}
    if not isinstance(data, dict):
        return []
    out: list[tuple[str, str, str, int]] = []

    for raw_cid, block in data.items():
        if not isinstance(block, dict):
            continue

        # Current Insight snapshot: each hero key contains the original Rone
        # response with ``data.records[*].data.build[*].equipid``.
        nested_data = block.get("data")
        if isinstance(nested_data, dict) and isinstance(nested_data.get("records"), list):
            out.extend(parse_rone_build_payload(block, equipment_by_id))
            continue

        # Backward-compatible support for older flattened snapshots.
        cid = str(block.get("hero_id") or raw_cid)
        builds = block.get("builds") or block.get("recommended_builds") or []
        if not isinstance(builds, list) or not builds:
            continue

        def quality(build):
            if not isinstance(build, dict):
                return 0.0
            try:
                games = float(build.get("games_played") or build.get("sample_size") or 0.0)
            except (TypeError, ValueError):
                games = 0.0
            try:
                wr = float(build.get("win_rate") or 0.0)
            except (TypeError, ValueError):
                wr = 0.0
            return games + wr * 10.0

        best = max((b for b in builds if isinstance(b, dict)), key=quality, default={})
        equipment = best.get("equipment") or best.get("equipment_ids") or best.get("items") or []
        if not isinstance(equipment, list):
            continue
        seen: set[str] = set()
        priority = 1
        for value in equipment:
            name = _equipment_name(value, equipment_by_id)
            if not name or name in seen:
                continue
            seen.add(name)
            out.append((cid, name, "", priority))
            priority += 1
    return out
