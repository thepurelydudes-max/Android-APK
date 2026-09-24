from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import db
from media_cache import CHAMPION_DIR, ITEM_DIR, create_fallback_champion_icon, ensure_cache_dirs, safe_name, sync_cached_image
from paths import APP_DIR
from localization import has_cyrillic, russian_hero_name, russian_item_name
from sources import (
    MLBBDEX_ITEMS,
    RONE_ACADEMY_RECOMMENDED,
    RONE_EQUIPMENT_EXPANDED,
    RONE_HERO_RANK,
    RONE_PUBLIC_HEROES,
    Net,
    clean,
    clean_item_name,
    clean_item_effect,
    fetch_mlbb_patch_info,
    fetch_mlbbdex_heroes,
    fetch_mlbbdex_items,
    fetch_mlbbdex_rankings,
    parse_rone_equipment,
    parse_rone_public_heroes,
    parse_rone_rank_payload,
    parse_rone_recommended_payload,
    slugish,
    apply_ru_localization,
    load_ru_localization,
    save_ru_localization,
)


class UpdateCancelled(RuntimeError):
    """Raised when a cooperative update cancellation is requested."""


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise UpdateCancelled("Update cancelled")


UPDATE_TEXT = {
    "ru": {
        "loading_champions": "1/6 Загружаю героев Mobile Legends…",
        "loading_stats": "2/6 Загружаю актуальные win/pick/ban и Tier…",
        "loading_matchups": "3/6 Загружаю матрицу контрпиков…",
        "loading_items": "4/6 Загружаю предметы и базовые сборки…",
        "loading_counter_items": "5/6 Строю ситуационные контрпредметы…",
        "caching_media": "6/6 Кэширую портреты и иконки…",
        "cache_champions": "Кэш портретов: {current}/{total}",
        "cache_items": "Кэш предметов: {current}/{total}",
        "done": "Обновление Mobile Legends завершено.",
    },
    "en": {
        "loading_champions": "1/6 Loading Mobile Legends heroes…",
        "loading_stats": "2/6 Loading current win/pick/ban and Tier…",
        "loading_matchups": "3/6 Loading counter-pick matrix…",
        "loading_items": "4/6 Loading items and core builds…",
        "loading_counter_items": "5/6 Building situational counter-items…",
        "caching_media": "6/6 Caching hero portraits and item icons…",
        "cache_champions": "Hero portraits: {current}/{total}",
        "cache_items": "Item icons: {current}/{total}",
        "done": "Mobile Legends update complete.",
    },
}


def update_text(key: str, lang: str = "ru", **values) -> str:
    lang = "ru" if str(lang).lower().startswith("ru") else "en"
    return UPDATE_TEXT[lang].get(key, key).format(**values)


def format_update_timestamp(value: datetime) -> str:
    return value.strftime("%d-%m-%Y %H:%M")


def _portable_path(path: str) -> str:
    from paths import portable_media_path
    return portable_media_path(path)


def _seed_media_record(asset_key: str, current_url: str, existing_row: dict | None, target: Path, previous_patch: str, current_patch: str) -> dict | None:
    record = db.get_media_asset(asset_key)
    if record:
        return record
    if target.exists():
        return {
            "source_url": (existing_row or {}).get("icon_url") or current_url,
            "etag": "",
            "last_modified": "",
            "content_length": 0,
            "sha256": "",
            "local_path": _portable_path(str(target)),
            "checked_patch": previous_patch or current_patch,
            "last_checked": "",
        }
    return None


def _store_media_result(asset_key: str, result) -> None:
    if not result.path:
        return
    db.upsert_media_asset(
        asset_key,
        source_url=result.source_url,
        etag=result.etag,
        last_modified=result.last_modified,
        content_length=result.content_length,
        sha256=result.sha256,
        local_path=_portable_path(result.path),
        checked_patch=result.checked_patch,
        last_checked=result.last_checked,
    )


def _infer_damage_type(roles: Iterable[str], explicit: str = "") -> str:
    if explicit:
        return explicit
    values = {str(x).casefold() for x in roles}
    if "mage" in values and not ({"marksman", "fighter", "assassin"} & values):
        return "AP"
    if {"marksman", "fighter", "assassin"} & values and "mage" not in values:
        return "AD"
    return ""


def merge_mlbb_hero_sources(
    rone_rows: list[dict], dex_rows: list[dict], rone_ru_rows: list[dict] | None = None,
) -> list[dict]:
    """Use Rone numeric IDs/relations, MLBBDex metadata and RU names by stable ID."""
    dex_by_name = {slugish(row.get("name", "")): row for row in dex_rows if row.get("name")}
    ru_by_id = {str(row.get("id") or ""): row for row in (rone_ru_rows or []) if row.get("id")}
    out: list[dict] = []
    used_names: set[str] = set()
    for rone in rone_rows:
        key = slugish(rone.get("name", ""))
        dex = dex_by_name.get(key, {})
        used_names.add(key)
        roles = list(dex.get("roles") or rone.get("roles") or [])
        lanes = [str(x).casefold() for x in (dex.get("lanes") or rone.get("lanes") or [])]
        cid = str(rone.get("id") or dex.get("id") or "")
        ru = ru_by_id.get(cid, {})
        out.append({
            "id": cid,
            "name": clean(str(rone.get("name") or dex.get("name") or "")),
            "name_ru": (clean(str(ru.get("name") or "")) if has_cyrillic(ru.get("name")) else russian_hero_name(cid, rone.get("name") or dex.get("name") or "")),
            "roles": roles,
            "lanes": lanes,
            "specialties": list(dex.get("specialties") or []),
            "damage_type": _infer_damage_type(roles, str(dex.get("damage_type") or "")),
            "icon_url": str(rone.get("icon_url") or dex.get("icon_url") or ""),
            "strong": [str(x) for x in (rone.get("strong") or [])],
            "weak": [str(x) for x in (rone.get("weak") or [])],
            "assist": [str(x) for x in (rone.get("assist") or [])],
        })
    # MLBBDex can be ahead of community APIs by a hero or two. Keep such heroes
    # selectable immediately; their matchup/build coverage will arrive when the
    # numeric source catches up.
    for dex in dex_rows:
        key = slugish(dex.get("name", ""))
        if not key or key in used_names:
            continue
        roles = list(dex.get("roles") or [])
        out.append({
            "id": str(dex.get("id") or key),
            "name": clean(str(dex.get("name") or "")),
            "name_ru": russian_hero_name(str(dex.get("id") or key), str(dex.get("name") or "")),
            "roles": roles,
            "lanes": [str(x).casefold() for x in (dex.get("lanes") or [])],
            "specialties": list(dex.get("specialties") or []),
            "damage_type": _infer_damage_type(roles, str(dex.get("damage_type") or "")),
            "icon_url": str(dex.get("icon_url") or ""),
            "strong": [], "weak": [], "assist": [],
        })
    return [row for row in out if row.get("id") and row.get("name")]


def build_resolver(champs: list[dict]):
    table: dict[str, str] = {}
    for c in champs:
        cid = str(c.get("id") or "")
        for value in (cid, c.get("name", ""), c.get("name_ru", "")):
            key = slugish(str(value or ""))
            if key:
                table[key] = cid

    def resolve(value: str | int | None):
        key = slugish(str(value or ""))
        if not key:
            return None
        if key in table:
            return table[key]
        matches = {cid for candidate, cid in table.items() if candidate.startswith(key) or key.startswith(candidate)}
        return next(iter(matches)) if len(matches) == 1 else None
    return resolve


def relation_matchups(heroes: list[dict]) -> list[tuple[str, str, str, float]]:
    edges: dict[tuple[str, str, str], float] = {}
    for hero in heroes:
        cid = str(hero.get("id") or "")
        if not cid:
            continue
        for victim in hero.get("strong") or []:
            victim = str(victim)
            if not victim or victim == cid:
                continue
            edges[(cid, victim, "")] = max(edges.get((cid, victim, ""), 0.0), 0.90)
            edges[(victim, cid, "")] = min(edges.get((victim, cid, ""), 0.0), -0.90)
        for counter in hero.get("weak") or []:
            counter = str(counter)
            if not counter or counter == cid:
                continue
            edges[(counter, cid, "")] = max(edges.get((counter, cid, ""), 0.0), 0.90)
            edges[(cid, counter, "")] = min(edges.get((cid, counter, ""), 0.0), -0.90)
    return [(a, b, role, score) for (a, b, role), score in edges.items()]


def _merge_matchups(*groups: Iterable[tuple[str, str, str, float]], valid_ids: set[str] | None = None) -> list[tuple[str, str, str, float]]:
    merged: dict[tuple[str, str, str], float] = {}
    for group in groups:
        for a, b, role, raw_score in group:
            a, b = str(a), str(b)
            if a == b or not a or not b:
                continue
            if valid_ids is not None and (a not in valid_ids or b not in valid_ids):
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            key = (a, b, str(role or ""))
            old = merged.get(key)
            if old is None or abs(score) > abs(old):
                merged[key] = score
    return [(a, b, role, score) for (a, b, role), score in merged.items()]


def _item_hash(row: dict) -> str:
    payload = {
        "name": row.get("name", ""), "price": int(row.get("price") or 0),
        "stats": list(row.get("stats") or []), "effect": row.get("effect_en", ""),
        "effect_ru": row.get("effect_ru", ""), "name_ru": row.get("name_ru", ""),
        "icon": row.get("icon_url", ""),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def apply_builtin_ru_item_localization(items: list[dict]) -> None:
    """Apply stable RU item names and reject Latin-only text masquerading as RU."""
    for item in items:
        english = str(item.get("name") or "").strip()
        builtin = russian_item_name(english)
        current_name = str(item.get("name_ru") or "").strip()
        if builtin:
            item["name_ru"] = builtin
        elif current_name and not has_cyrillic(current_name):
            item["name_ru"] = ""

        effect_ru = str(item.get("effect_ru") or "").strip()
        if effect_ru and not has_cyrillic(effect_ru):
            # RU UI will derive a compact Russian description from canonical effect_en.
            item["effect_ru"] = ""


def merge_rone_item_localization(rone_en_rows: list[dict], rone_ru_rows: list[dict]) -> list[dict]:
    """Attach Russian item names/effects to canonical English Rone rows by equip ID."""
    ru_by_id = {str(row.get("id") or ""): row for row in rone_ru_rows if row.get("id")}
    out: list[dict] = []
    for row in rone_en_rows:
        merged = dict(row)
        ru = ru_by_id.get(str(row.get("id") or ""), {})
        if ru:
            ru_name = clean_item_name(str(ru.get("name") or ""))
            ru_effect = clean_item_effect(str(ru.get("effect_en") or ru.get("effect_ru") or ""))
            merged["name_ru"] = ru_name if has_cyrillic(ru_name) else ""
            merged["effect_ru"] = ru_effect if has_cyrillic(ru_effect) else ""
        else:
            merged.setdefault("name_ru", "")
            merged.setdefault("effect_ru", "")
        out.append(merged)
    return out


def _merge_localization_payload(previous: dict, heroes: list[dict], items: list[dict]) -> dict:
    """Merge fresh localized data without persisting Latin-only pseudo-RU text."""
    previous = previous or {}
    payload = {"heroes": {}, "items": {}}

    for cid, old in dict(previous.get("heroes") or {}).items():
        if not isinstance(old, dict):
            continue
        name_ru = str(old.get("name_ru") or "").strip()
        if has_cyrillic(name_ru):
            payload["heroes"][str(cid)] = {
                "name_en": str(old.get("name_en") or ""),
                "name_ru": name_ru,
            }

    for name, old in dict(previous.get("items") or {}).items():
        if not isinstance(old, dict):
            continue
        name_ru = str(old.get("name_ru") or "").strip()
        effect_ru = str(old.get("effect_ru") or "").strip()
        name_ru = name_ru if has_cyrillic(name_ru) else ""
        effect_ru = effect_ru if has_cyrillic(effect_ru) else ""
        if name_ru or effect_ru:
            payload["items"][str(name)] = {"name_ru": name_ru, "effect_ru": effect_ru}

    for hero in heroes:
        cid = str(hero.get("id") or "")
        name_ru = str(hero.get("name_ru") or "").strip()
        if cid and has_cyrillic(name_ru):
            payload["heroes"][cid] = {
                "name_en": str(hero.get("name") or ""),
                "name_ru": name_ru,
            }
    for item in items:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        name_ru = str(item.get("name_ru") or "").strip()
        effect_ru = str(item.get("effect_ru") or "").strip()
        name_ru = name_ru if has_cyrillic(name_ru) else ""
        effect_ru = effect_ru if has_cyrillic(effect_ru) else ""
        old = payload["items"].get(name, {}) if isinstance(payload["items"].get(name), dict) else {}
        if name_ru or effect_ru or old:
            payload["items"][name] = {
                "name_ru": name_ru or str(old.get("name_ru") or ""),
                "effect_ru": effect_ru or str(old.get("effect_ru") or ""),
            }
    return payload


def _merge_item_sources(dex_items: list[dict], rone_items: list[dict]) -> list[dict]:
    rone_by_name = {slugish(row.get("name", "")): row for row in rone_items if row.get("name")}
    out: list[dict] = []
    for row in dex_items:
        merged = dict(row)
        rone = rone_by_name.get(slugish(row.get("name", "")), {})
        if rone.get("icon_url"):
            merged["icon_url"] = rone["icon_url"]
        if rone.get("category") and not merged.get("category"):
            merged["category"] = rone["category"]
        if rone.get("stats"):
            merged["stats"] = list(dict.fromkeys([*(merged.get("stats") or []), *(rone.get("stats") or [])]))
        if rone.get("effect_en") and not merged.get("effect_en"):
            merged["effect_en"] = rone["effect_en"]
        if rone.get("name_ru"):
            merged["name_ru"] = rone["name_ru"]
        if rone.get("effect_ru"):
            merged["effect_ru"] = rone["effect_ru"]
        out.append(merged)
    return out


def _filter_build_pool(rows: Iterable[tuple[str, str, str, int]], valid_ids: set[str], item_by_slug: dict[str, dict]) -> list[tuple[str, str, str, int]]:
    best: dict[tuple[str, str], tuple[str, str, str, int]] = {}
    for cid, raw_name, _category, priority in rows:
        cid = str(cid)
        item = item_by_slug.get(slugish(raw_name))
        if cid not in valid_ids or not item:
            continue
        name = item["name"]
        key = (cid, slugish(name))
        value = (cid, name, str(item.get("category") or ""), int(priority or 999))
        old = best.get(key)
        if old is None or value[3] < old[3]:
            best[key] = value
    return sorted(best.values(), key=lambda row: (row[0], row[3], row[1]))


def generate_counter_item_rows(heroes: list[dict], existing_items: set[str]) -> list[tuple[str, str, str]]:
    """Generate MLBB-specific situational answers from enemy archetypes."""
    by_slug = {slugish(name): name for name in existing_items}
    rows: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(cid: str, wanted: str, reason: str) -> None:
        actual = by_slug.get(slugish(wanted))
        key = (cid, actual or "")
        if actual and key not in seen:
            seen.add(key)
            rows.append((cid, actual, reason))

    for hero in heroes:
        cid = str(hero.get("id") or "")
        roles = {str(x).casefold() for x in (hero.get("roles") or [])}
        specialties = {str(x).casefold() for x in (hero.get("specialties") or [])}
        if not cid:
            continue
        if "mage" in roles:
            add(cid, "Athena's Shield", "защита от магического взрывного урона")
            add(cid, "Radiant Armor", "защита от постоянного магического урона")
        if "marksman" in roles:
            add(cid, "Blade Armor", "защита от автоатак и критического урона")
            add(cid, "Chastise Pauldron", "снижает скорость атаки противника")
        if {"fighter", "assassin"} & roles:
            add(cid, "Antique Cuirass", "защита от физического урона навыками")
            add(cid, "Immortality", "страховка против взрывного урона")
        if "tank" in roles:
            add(cid, "Malefic Roar", "физическое пробивание против высокой защиты")
            add(cid, "Demon Hunter Sword", "урон по героям с большим запасом здоровья")
            add(cid, "Divine Glaive", "магическое пробивание против высокой защиты")
            add(cid, "Wishing Lantern", "магический урон по плотной цели")
        if "support" in roles or specialties & {"regen", "heal", "healing", "sustain"}:
            add(cid, "Dominance Ice", "антихил и защитный ответ")
            add(cid, "Sea Halberd", "антихил для физического героя")
            add(cid, "Glowing Wand", "антихил для магического героя")
        if "support" in roles or specialties & {"control", "crowd control", "cc", "initiator"}:
            add(cid, "Tough Boots", "сопротивление магии и контролю")
        if specialties & {"burst", "reap"}:
            add(cid, "Immortality", "страховка против взрывного добивания")
    return rows


def _cache_media(
    net: Net, champs: list[dict], items: list[dict], progress: Callable[[str], None],
    lang: str, current_patch: str, previous_patch: str, errors: list[str],
    cancel_check: Callable[[], bool] | None,
) -> tuple[int, int]:
    ensure_cache_dirs()
    champion_count = 0
    item_count = 0
    for index, champ in enumerate(champs, 1):
        _check_cancel(cancel_check)
        url = str(champ.get("icon_url") or "")
        target = CHAMPION_DIR / f"{safe_name(str(champ['id']))}.png"
        if not url:
            fallback = create_fallback_champion_icon(target, str(champ.get("name") or champ.get("name_ru") or champ["id"]))
            if fallback:
                db.update_champion_media(str(champ["id"]), "", _portable_path(fallback))
                champion_count += 1
            continue
        existing = db.champion_by_name_or_id(str(champ["id"]))
        record = _seed_media_record(f"champion:{champ['id']}", url, existing, target, previous_patch, current_patch)
        result = sync_cached_image(net, url, target, record, current_patch=current_patch, previous_patch=previous_patch)
        _store_media_result(f"champion:{champ['id']}", result)
        if result.path:
            db.update_champion_media(str(champ["id"]), result.source_url or url, _portable_path(result.path))
            champion_count += 1
        elif result.status == "failed":
            fallback = create_fallback_champion_icon(target, str(champ.get("name") or champ.get("name_ru") or champ["id"]))
            if fallback:
                db.update_champion_media(str(champ["id"]), "", _portable_path(fallback))
                champion_count += 1
            else:
                errors.append(f"Hero image {champ.get('name', champ['id'])}: failed")
        if index % 20 == 0:
            progress(update_text("cache_champions", lang, current=index, total=len(champs)))

    for index, item in enumerate(items, 1):
        _check_cancel(cancel_check)
        name = str(item.get("name") or "")
        url = str(item.get("icon_url") or "")
        if not name or not url:
            continue
        target = ITEM_DIR / f"{safe_name(name)}.png"
        existing = db.get_item(name)
        record = _seed_media_record(f"item:{name}", url, existing, target, previous_patch, current_patch)
        result = sync_cached_image(net, url, target, record, current_patch=current_patch, previous_patch=previous_patch)
        _store_media_result(f"item:{name}", result)
        if result.path:
            db.update_item_media(name, result.source_url or url, _portable_path(result.path))
            item_count += 1
        elif result.status == "failed":
            errors.append(f"Item image {name}: failed")
        if index % 30 == 0:
            progress(update_text("cache_items", lang, current=index, total=len(items)))
    return champion_count, item_count


def update_all(
    progress: Callable[[str], None] | None = None,
    lang: str = "ru",
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    _check_cancel(cancel_check)
    db.init_db()
    raw_progress = progress or (lambda _s: None)

    def emit(message: str) -> None:
        _check_cancel(cancel_check)
        raw_progress(message)

    net = Net(delay=0.08)
    now = datetime.now().astimezone()
    now_iso = now.isoformat()
    previous_patch = db.get_meta("patch_version", "")
    current_patch = previous_patch
    localization_path = APP_DIR / "localization_ru.json"
    ru_localization = load_ru_localization(localization_path)
    summary = {
        "champions": 0, "stats": 0, "matchups": 0, "item_pool": 0,
        "counter_items": 0, "champion_images": 0, "item_images": 0,
        "item_details_changed": 0, "patch": "", "errors": [],
    }

    # 1) Heroes: current numeric identity/relations from Rone, metadata from MLBBDex.
    emit(update_text("loading_champions", lang))
    rone_heroes: list[dict] = []
    rone_heroes_ru: list[dict] = []
    dex_heroes: list[dict] = []
    try:
        payload = net.get(f"{RONE_PUBLIC_HEROES}?size=300&index=1&lang=en").json()
        rone_heroes = parse_rone_public_heroes(payload)
    except Exception as exc:
        summary["errors"].append(f"Rone heroes: {exc}")
    _check_cancel(cancel_check)
    try:
        payload_ru = net.get(f"{RONE_PUBLIC_HEROES}?size=300&index=1&lang=ru").json()
        rone_heroes_ru = parse_rone_public_heroes(payload_ru)
    except Exception as exc:
        summary["errors"].append(f"Rone RU heroes: {exc}")
    _check_cancel(cancel_check)
    try:
        dex_heroes = fetch_mlbbdex_heroes(net)
    except Exception as exc:
        summary["errors"].append(f"MLBBDex heroes: {exc}")
    champs = merge_mlbb_hero_sources(rone_heroes, dex_heroes, rone_heroes_ru)
    apply_ru_localization(champs, [], ru_localization)
    if not champs:
        # Never destroy a previously working offline database when all hero
        # providers are temporarily unavailable.
        champs = db.champions()
        if not champs:
            raise RuntimeError("Не удалось получить список героев Mobile Legends из Rone Arena или MLBBDex")
    for champ in champs:
        db.upsert_champion(
            str(champ["id"]), str(champ["name"]), list(champ.get("roles") or []),
            list(champ.get("lanes") or []), str(champ.get("damage_type") or ""),
            "Rone Arena + MLBBDex", now_iso, name_ru=str(champ.get("name_ru") or ""),
            icon_url=str(champ.get("icon_url") or ""),
        )
    summary["champions"] = len(champs)
    valid_ids = {str(c["id"]) for c in champs}
    resolve = build_resolver(champs)

    try:
        patch, patch_date = fetch_mlbb_patch_info(net)
        if patch:
            current_patch = patch
            summary["patch"] = patch
            db.set_meta("patch_version", patch)
        if patch_date:
            db.set_meta("patch_date", patch_date)
    except Exception as exc:
        summary["errors"].append(f"Patch version: {exc}")

    # 2) Stats + meta tier.
    # Rone remains the freshest 7-day source for win/pick/ban. MLBBDex is fetched
    # independently because its public rankings endpoint also publishes the
    # calculated S+/S/A/B/C/D tier and the measurement date.
    emit(update_text("loading_stats", lang))
    stat_rows: list[dict] = []
    dex_stats: list[dict] = []
    try:
        payload = net.get(f"{RONE_HERO_RANK}?days=7&rank=all&size=300&index=1&lang=en").json()
        stat_rows = parse_rone_rank_payload(payload)
    except Exception as exc:
        summary["errors"].append(f"Rone rank stats: {exc}")

    try:
        dex_stats = fetch_mlbbdex_rankings(net)
    except Exception as exc:
        summary["errors"].append(f"MLBBDex rankings/tier: {exc}")

    if not stat_rows:
        for row in dex_stats:
            cid = resolve(row.get("id") or row.get("name"))
            if cid:
                stat_rows.append({"champion_id": cid, **row})

    stored_stats = 0
    for row in stat_rows:
        cid = resolve(row.get("champion_id") or row.get("id") or row.get("name"))
        champ = next((c for c in champs if c["id"] == cid), None) if cid else None
        if not cid or not champ:
            continue
        lanes = list(champ.get("lanes") or []) or [""]
        for lane in lanes:
            db.upsert_stat(cid, str(lane).casefold(), "all", row.get("win_rate"), row.get("pick_rate"), row.get("ban_rate"), str(row.get("date") or ""))
            stored_stats += 1
    summary["stats"] = stored_stats

    stored_tiers = 0
    tier_dates: list[str] = []
    for row in dex_stats:
        cid = resolve(row.get("id") or row.get("name"))
        champ = next((c for c in champs if c["id"] == cid), None) if cid else None
        tier = str(row.get("tier") or "").strip().upper()
        if not cid or not champ or tier not in {"S+", "S", "A", "B", "C", "D"}:
            continue
        tier_date = str(row.get("date") or "")
        if tier_date:
            tier_dates.append(tier_date)
        lanes = list(champ.get("lanes") or []) or [""]
        for lane in lanes:
            db.upsert_stat_tier(
                cid,
                str(lane).casefold(),
                "all",
                tier,
                tier_date,
                "MLBBDex /api/v1/rankings",
            )
            stored_tiers += 1
    summary["tiers"] = stored_tiers
    if stored_tiers:
        db.set_meta("tier_source", "MLBBDex /api/v1/rankings")
        if tier_dates:
            db.set_meta("tier_date", max(tier_dates))

    # 3) Matchups: current Rone relations + live Academy hints.
    # The old Rafael-VH/Insight-Data-MLBB fallback was removed upstream and now
    # returns 404, so it is no longer queried.
    emit(update_text("loading_matchups", lang))
    relation_rows = relation_matchups(champs)

    # 4) Items and builds. MLBBDex defines the final shop catalog; Rone adds icons/details.
    emit(update_text("loading_items", lang))
    rone_items: list[dict] = []
    rone_items_ru: list[dict] = []
    equipment_by_id: dict[int, str] = {}
    try:
        payload = net.get(f"{RONE_EQUIPMENT_EXPANDED}?size=300&index=1&lang=en").json()
        rone_items, equipment_by_id = parse_rone_equipment(payload)
    except Exception as exc:
        summary["errors"].append(f"Rone equipment: {exc}")
    try:
        payload_ru = net.get(f"{RONE_EQUIPMENT_EXPANDED}?size=300&index=1&lang=ru").json()
        rone_items_ru, _ = parse_rone_equipment(payload_ru)
    except Exception as exc:
        summary["errors"].append(f"Rone RU equipment: {exc}")
    rone_items = merge_rone_item_localization(rone_items, rone_items_ru)
    try:
        dex_items = fetch_mlbbdex_items(net)
    except Exception as exc:
        dex_items = []
        summary["errors"].append(f"MLBBDex items: {exc}")
    items = _merge_item_sources(dex_items, rone_items) if dex_items else []
    apply_ru_localization([], items, ru_localization)
    apply_builtin_ru_item_localization(items)
    if items:
        db.replace_source_items(
            "mlbb.catalog",
            [(row["name"], row.get("category", ""), row.get("name_ru", ""), row.get("icon_url", ""), "Upgraded") for row in items],
        )
        for row in items:
            changed = db.upsert_item_details(
                row["name"], price=int(row.get("price") or 0), stats=list(row.get("stats") or []),
                effect_en=str(row.get("effect_en") or ""), effect_ru=str(row.get("effect_ru") or ""),
                data_hash=_item_hash(row), data_patch=current_patch, source_url=MLBBDEX_ITEMS,
            )
            summary["item_details_changed"] += int(bool(changed))
    else:
        # Keep last known-good final catalog on transient provider failure.
        items = [dict(row) for row in db.item_catalog_rows() if str(row.get("tier") or "").casefold() == "upgraded"]
        apply_builtin_ru_item_localization(items)
    item_by_slug = {slugish(row.get("name", "")): row for row in items if row.get("name")}

    live_pools: list[tuple[str, str, str, int]] = []
    live_matchups: list[tuple[str, str, str, float]] = []
    try:
        recommended = net.get(f"{RONE_ACADEMY_RECOMMENDED}?size=300&index=1&order=desc&lang=en").json()
        live_pools, live_matchups = parse_rone_recommended_payload(recommended, equipment_by_id)
    except Exception as exc:
        summary["errors"].append(f"Rone Academy recommendations: {exc}")
    # Rone Academy is the live build source. The former Insight fallback was
    # removed upstream and is intentionally not queried anymore.
    pools = _filter_build_pool(live_pools, valid_ids, item_by_slug)
    db.replace_source_item_pools("mlbb.builds", pools)
    summary["item_pool"] = len(pools)

    matchups = _merge_matchups(relation_rows, live_matchups, valid_ids=valid_ids)
    db.replace_source_matchups("mlbb.matchups", matchups)
    summary["matchups"] = len(matchups)

    # 5) MLBB-specific item adaptation rules.
    emit(update_text("loading_counter_items", lang))
    counter_items = generate_counter_item_rows(champs, {row["name"] for row in items if row.get("name")})
    db.replace_source_counter_items("mlbb.rules", counter_items)
    summary["counter_items"] = len(counter_items)

    # 6) Cache all available media locally so normal usage is fully offline.
    emit(update_text("caching_media", lang))
    ci, ii = _cache_media(
        net, champs, items, emit, lang, current_patch, previous_patch,
        summary["errors"], cancel_check,
    )
    summary["champion_images"] = ci
    summary["item_images"] = ii

    _check_cancel(cancel_check)
    try:
        save_ru_localization(localization_path, _merge_localization_payload(ru_localization, champs, items))
    except Exception as exc:
        summary["errors"].append(f"RU localization cache: {exc}")
    db.set_meta("last_update", format_update_timestamp(now))
    db.set_meta("last_update_iso", now_iso)
    db.set_meta(
        "source_note",
        "Mobile Legends data: Rone Arena API (unofficial) + MLBBDex. "
        "Internet is used only during Update; runtime recommendations use the local SQLite database and cache.",
    )
    raw_progress(update_text("done", lang))
    return summary
