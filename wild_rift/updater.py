from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

import db
from localization import COMMON_CHAMPION_ALIASES
from media_cache import BRAND_DIR, CHAMPION_DIR, ITEM_DIR, cache_brand_logo, ensure_cache_dirs, safe_name, sync_cached_image
from sources import (
    DDRAGON_CHAMPION_ICON, Net, fetch_champions_locale, fetch_counter_item_pages,
    fetch_ddragon_item_ru_map, fetch_ddragon_version, fetch_stats, fetch_wrpocket_item_pools,
    fetch_wrpocket_item_dataset, fetch_wrpocket_item_detail_dataset,
    item_detail_fallback_names, fetch_current_patch_info, item_name_ru,
    parse_wildriftcore_matchups, parse_wildriftcore_tiers, slugish, clean_item_name, clean_wrpocket_item_stats,
    clean_wrpocket_item_effect, _item_dataset_hash, canonical_item_name, is_finished_item_tier,
)


ITEM_DATA_SCHEMA_VERSION = "3"


class UpdateCancelled(RuntimeError):
    """Raised when a cooperative update cancellation is requested."""


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise UpdateCancelled("Update cancelled")


UPDATE_TEXT = {
    "ru": {
        "loading_champions": "1/7 Загружаю чемпионов EN/RU…",
        "loading_stats": "2/7 Загружаю актуальные win/pick/ban…",
        "loading_tiers": "3/7 Загружаю тиры чемпионов WildRiftCore…",
        "loading_matchups": "4/7 Загружаю матрицу контрпиков…",
        "loading_items": "5/7 Загружаю предметы и русские названия…",
        "loading_counter_items": "6/7 Загружаю предметы-контрмеры…",
        "caching_media": "7/7 Кэширую портреты и иконки…",
        "cache_champions": "Кэш портретов: {current}/{total}",
        "cache_items": "Кэш предметов: {current}/{total}",
        "done": "Обновление завершено.",
    },
    "en": {
        "loading_champions": "1/7 Loading champions EN/RU…",
        "loading_stats": "2/7 Loading current win/pick/ban…",
        "loading_tiers": "3/7 Loading WildRiftCore champion tiers…",
        "loading_matchups": "4/7 Loading counter-pick matrix…",
        "loading_items": "5/7 Loading items and localized names…",
        "loading_counter_items": "6/7 Loading counter-items…",
        "caching_media": "7/7 Caching champion portraits and item icons…",
        "cache_champions": "Champion portraits: {current}/{total}",
        "cache_items": "Item icons: {current}/{total}",
        "done": "Update complete.",
    },
}


def update_text(key: str, lang: str = "ru", **values) -> str:
    lang = "ru" if str(lang).lower().startswith("ru") else "en"
    template = UPDATE_TEXT[lang].get(key, key)
    return template.format(**values)


def format_update_timestamp(value: datetime) -> str:
    return value.strftime("%d-%m-%Y %H:%M")


def merge_champion_locales(en_rows: list[dict], ru_rows: list[dict]) -> list[dict]:
    ru_by_id = {c["id"]: c for c in ru_rows}
    out = []
    for row in en_rows:
        merged = dict(row)
        merged["name_ru"] = (ru_by_id.get(row["id"]) or {}).get("name", "")
        out.append(merged)
    return out


def build_resolver(champs: list[dict]):
    table: dict[str, str] = {}
    manual = {
        "khazix": "Kha'Zix", "kaisa": "Kai'Sa", "kogmaw": "Kog'Maw", "chogath": "Cho'Gath",
        "drmundo": "DrMundo", "nunuandwillump": "Nunu", "jarvaniv": "JarvanIV", "monkeyking": "Wukong",
    }
    for c in champs:
        cid = c["id"]
        for v in (cid, c.get("name", ""), c.get("name_ru", "")):
            if v:
                table[slugish(v)] = cid
    for alias, wanted in manual.items():
        for c in champs:
            if slugish(c["id"]) == slugish(wanted) or slugish(c.get("name", "")) == slugish(wanted):
                table[alias] = c["id"]
                break

    def resolve(value: str):
        key = slugish(value)
        if not key:
            return None
        if key in table:
            return table[key]
        candidates = [cid for k, cid in table.items() if k.startswith(key) or key.startswith(k)]
        return candidates[0] if len(set(candidates)) == 1 else None
    return resolve


def _portable_path(path: str) -> str:
    from paths import portable_media_path
    return portable_media_path(path)


def _canonicalize_item_pools(pools: list[tuple[str, str, str, int]], items: list[tuple[str, str, str]]) -> list[tuple[str, str, str, int]]:
    by_slug = {slugish(canonical_item_name(row[0])): canonical_item_name(row[0]) for row in items}
    out = []
    for cid, raw, cat, priority in pools:
        clean_name = canonical_item_name(raw)
        canonical = by_slug.get(slugish(clean_name), clean_name)
        out.append((cid, canonical, cat, priority))
    return out


def _filter_finished_item_pools(
    pools: list[tuple[str, str, str, int]], finished_names: set[str],
) -> list[tuple[str, str, str, int]]:
    """Hard safety gate: final builds may contain only catalog items marked Upgraded."""
    allowed = {slugish(canonical_item_name(name)) for name in finished_names if canonical_item_name(name)}
    out: list[tuple[str, str, str, int]] = []
    for cid, raw, cat, priority in pools:
        name = canonical_item_name(raw)
        if slugish(name) in allowed:
            out.append((cid, name, cat, priority))
    return out


def _seed_media_record(asset_key: str, current_url: str, existing_row: dict | None, target: Path, previous_patch: str, current_patch: str) -> dict | None:
    record = db.get_media_asset(asset_key)
    if record:
        return record
    # Migration path for users upgrading from the pre-manifest cache: preserve a
    # valid existing image and treat its stored/current URL as the baseline.
    if target.exists():
        return {
            "source_url": (existing_row or {}).get("icon_url") or current_url,
            "etag": "", "last_modified": "", "content_length": 0, "sha256": "",
            "local_path": _portable_path(str(target)),
            "checked_patch": previous_patch or current_patch,
            "last_checked": "",
        }
    return None


def _store_media_result(asset_key: str, result) -> None:
    if not result.path:
        return
    db.upsert_media_asset(
        asset_key, source_url=result.source_url, etag=result.etag, last_modified=result.last_modified,
        content_length=result.content_length, sha256=result.sha256, local_path=_portable_path(result.path),
        checked_patch=result.checked_patch, last_checked=result.last_checked,
    )


def _item_dataset_headers() -> dict[str, str]:
    if db.get_meta("item_dataset_schema_version", "") != ITEM_DATA_SCHEMA_VERSION:
        # Parser schema changed: force one fresh body so old polluted tooltip
        # records are rebuilt even if the server still considers the page 304.
        return {}
    headers: dict[str, str] = {}
    etag = db.get_meta("item_dataset_etag", "")
    modified = db.get_meta("item_dataset_last_modified", "")
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    return headers


def _catalog_items_for_wanted(wanted_items: set[str]) -> list[tuple[str, str, str]]:
    wanted_slugs = {slugish(clean_item_name(x)) for x in wanted_items if clean_item_name(x)}
    rows = []
    for item in db.item_catalog_rows():
        if wanted_slugs and slugish(clean_item_name(item.get("name", ""))) not in wanted_slugs:
            continue
        rows.append((item.get("name", ""), item.get("category", ""), item.get("icon_url", "")))
    return rows


def _catalog_finished_items() -> list[tuple[str, str, str]]:
    """Return the complete local WR Pocket catalog of final, fully upgraded items."""
    rows: list[tuple[str, str, str]] = []
    for item in db.item_catalog_rows():
        if not is_finished_item_tier(str(item.get("tier") or "")):
            continue
        rows.append((item.get("name", ""), item.get("category", ""), item.get("icon_url", "")))
    return rows


def _replace_finished_catalog(records: list[dict], pc_ru: dict[str, str]) -> bool:
    """Prune stale/components only when the full catalog parse looks complete."""
    rows = []
    seen = set()
    for row in records:
        if not is_finished_item_tier(str(row.get("tier") or "")):
            continue
        name = canonical_item_name(row.get("name", ""))
        key = slugish(name)
        if not name or key in seen:
            continue
        seen.add(key)
        rows.append((
            name, str(row.get("category") or ""), item_name_ru(name, pc_ru),
            str(row.get("icon_url") or ""), "Upgraded",
        ))

    # WR currently has far more than fifty final items.  If a layout change makes
    # the parser see only a fragment, keep the previous catalog instead of
    # destructively deleting most known-good rows.
    existing_count = len(_catalog_finished_items())
    minimum = max(50, int(existing_count * 0.60)) if existing_count else 50
    if len(rows) < minimum:
        return False
    db.replace_source_items("wrpocket.app", rows)
    return True


def _apply_item_dataset(records: list[dict], patch: str, pc_ru: dict[str, str]) -> tuple[list[tuple[str, str, str]], int]:
    """Store only changed item rows; unchanged hashes cause no item-detail UPDATE."""
    items: list[tuple[str, str, str]] = []
    changed = 0
    for row in records:
        tier = str(row.get("tier") or "")
        if not is_finished_item_tier(tier):
            continue
        name = canonical_item_name(row.get("name", ""))
        if not name:
            continue
        icon_url = str(row.get("icon_url") or "")
        items.append((name, str(row.get("category") or ""), icon_url))
        existing = db.get_item(name)
        ru_name = item_name_ru(name, pc_ru)
        if existing is None:
            db.upsert_item(name, str(row.get("category") or ""), "wrpocket.app", name_ru=ru_name, icon_url=icon_url, tier=tier)
        else:
            catalog_changed = (
                (ru_name and ru_name != (existing.get("name_ru") or "")) or
                (icon_url and icon_url != (existing.get("icon_url") or "")) or
                str(row.get("category") or "") != str(existing.get("category") or "") or
                (tier and tier != str(existing.get("tier") or ""))
            )
            if catalog_changed:
                db.upsert_item(name, str(row.get("category") or ""), "wrpocket.app", name_ru=ru_name, icon_url=icon_url, tier=tier)

        price = int(row.get("price") or 0)
        stats = clean_wrpocket_item_stats(list(row.get("stats") or []))
        effect_en = clean_wrpocket_item_effect(str(row.get("effect_en") or ""))
        if existing:
            if price <= 0:
                try:
                    price = int(existing.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0
            if not stats:
                try:
                    import json
                    old_stats = json.loads(existing.get("stats_json") or "[]")
                except Exception:
                    old_stats = []
                stats = clean_wrpocket_item_stats(old_stats)
            if not effect_en:
                effect_en = clean_wrpocket_item_effect(str(existing.get("effect_en") or ""))
        detail_url = str(row.get("detail_url") or "")
        data_hash = _item_dataset_hash(name, price, stats, effect_en, icon_url, detail_url)
        if db.upsert_item_details(
            name,
            price=price,
            stats=stats,
            effect_en=effect_en,
            data_hash=data_hash,
            data_patch=patch,
            source_url=detail_url,
        ):
            changed += 1
    return items, changed


def _cache_media(
    net: Net, champs: list[dict], items: list[tuple], version: str, progress: Callable[[str], None],
    lang: str = "ru", current_patch: str = "", previous_patch: str = "",
    errors: list[str] | None = None, cancel_check: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    ensure_cache_dirs()
    champ_count = 0
    item_count = 0
    errors = errors if errors is not None else []
    for idx, c in enumerate(champs, 1):
        _check_cancel(cancel_check)
        target = CHAMPION_DIR / f"{safe_name(c['id'])}.png"
        existing = db.champion_by_name_or_id(c["id"])
        url = (DDRAGON_CHAMPION_ICON.format(version=version, champion_id=c["id"])
               if version else (existing or {}).get("icon_url", ""))
        if not url:
            errors.append(f"Portrait {c['id']}: no download URL")
            continue
        record = _seed_media_record(f"champion:{c['id']}", url, existing, target, previous_patch, current_patch)
        try:
            result = sync_cached_image(
                net, url, target, record, current_patch=current_patch, previous_patch=previous_patch,
            )
            if result.status in {"failed", "stale_kept"}:
                errors.append(f"Portrait {c['id']}: {result.status}")
            _store_media_result(f"champion:{c['id']}", result)
            if result.path:
                db.update_champion_media(c["id"], result.source_url or url, _portable_path(result.path))
                champ_count += 1
        except Exception as exc:
            errors.append(f"Portrait {c['id']}: {exc}")
        if idx % 20 == 0:
            progress(update_text("cache_champions", lang, current=idx, total=len(champs)))

    for idx, row in enumerate(items, 1):
        _check_cancel(cancel_check)
        name = row[0]
        icon_url = row[2] if len(row) > 2 else ""
        if not icon_url:
            continue
        target = ITEM_DIR / f"{safe_name(name)}.png"
        existing = db.get_item(name)
        record = _seed_media_record(f"item:{name}", icon_url, existing, target, previous_patch, current_patch)
        try:
            result = sync_cached_image(
                net, icon_url, target, record, current_patch=current_patch, previous_patch=previous_patch,
            )
            if result.status in {"failed", "stale_kept"}:
                errors.append(f"Item image {name}: {result.status}")
            _store_media_result(f"item:{name}", result)
            if result.path:
                db.update_item_media(name, result.source_url or icon_url, _portable_path(result.path))
                item_count += 1
        except Exception as exc:
            errors.append(f"Item image {name}: {exc}")
        if idx % 30 == 0:
            progress(update_text("cache_items", lang, current=idx, total=len(items)))

    _check_cancel(cancel_check)
    try:
        logo_path = cache_brand_logo(net)
        if logo_path:
            db.set_meta("brand_logo_path", _portable_path(logo_path))
    except Exception:
        pass
    return champ_count, item_count


def update_all(
    progress: Callable[[str], None] | None = None,
    lang: str = "ru",
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    _check_cancel(cancel_check)
    db.init_db()
    raw_progress = progress or (lambda s: None)

    def emit(message: str) -> None:
        _check_cancel(cancel_check)
        raw_progress(message)

    net = Net()
    summary = {"champions": 0, "stats": 0, "tiers": 0, "matchups": 0, "item_pool": 0, "counter_items": 0, "champion_images": 0, "item_images": 0, "item_details_changed": 0, "patch": "", "errors": []}
    previous_patch = db.get_meta("patch_version", "")
    current_patch = previous_patch

    emit(update_text("loading_champions", lang))
    en_champs = fetch_champions_locale(net, "en_US")
    _check_cancel(cancel_check)
    try:
        ru_champs = fetch_champions_locale(net, "ru_RU")
        _check_cancel(cancel_check)
    except UpdateCancelled:
        raise
    except Exception as e:
        ru_champs = []
        summary["errors"].append(f"RU champions: {e}")
    champs = merge_champion_locales(en_champs, ru_champs)
    now = datetime.now().astimezone()
    now_iso = now.isoformat()
    for c in champs:
        db.upsert_champion(c["id"], c["name"], c["roles"], c["lanes"], c["damage_type"], "ry2x/WildRift-Merged-Champion-Data", now_iso, name_ru=c.get("name_ru", ""))
        extras = COMMON_CHAMPION_ALIASES.get(c["id"], ())
        if extras:
            db.replace_champion_aliases(c["id"], extras)
    summary["champions"] = len(champs)
    resolve = build_resolver(champs)
    try:
        patch, patch_date = fetch_current_patch_info(net)
        _check_cancel(cancel_check)
        summary["patch"] = patch
        if patch:
            current_patch = patch
            db.set_meta("patch_version", patch)
        if patch_date:
            db.set_meta("patch_date", patch_date)
    except UpdateCancelled:
        raise
    except Exception as e:
        summary["errors"].append(f"Patch version: {e}")

    emit(update_text("loading_stats", lang))
    try:
        date, stats = fetch_stats(net)
        _check_cancel(cancel_check)
        for s in stats:
            cid = resolve(s["champion_id"])
            if cid:
                db.upsert_stat(cid, s["lane"], s["rank"], s["win_rate"], s["pick_rate"], s["ban_rate"], date)
        summary["stats"] = len(stats)
        db.set_meta("stats_date", date)
    except UpdateCancelled:
        raise
    except Exception as e:
        summary["errors"].append(f"Stats API: {e}")

    emit(update_text("loading_tiers", lang))
    try:
        tiers = parse_wildriftcore_tiers(net, resolve, emit)
        _check_cancel(cancel_check)
        db.replace_source_champion_tiers_partial("wildriftcore.com", tiers, current_patch)
        summary["tiers"] = len(tiers)
        db.set_meta("tier_patch", current_patch)
    except UpdateCancelled:
        raise
    except Exception as e:
        # Keep the previously downloaded tier table if the site is temporarily
        # unavailable. The engine simply applies no tier bonus when a role row
        # is absent.
        summary["errors"].append(f"WildRiftCore tiers: {e}")

    emit(update_text("loading_matchups", lang))
    try:
        matchups = parse_wildriftcore_matchups(net, resolve, emit)
        _check_cancel(cancel_check)
        # Remove the legacy general matrix first. Keeping both sources would let
        # old ±1 records compete with the new role-specific −3..+3 verdicts.
        db.replace_source_matchups("wildriftcounter.com", [])
        db.replace_source_matchups("wildriftcore.com", matchups)
        summary["matchups"] = len(matchups)
    except UpdateCancelled:
        raise
    except Exception as e:
        summary["errors"].append(f"WildRiftCore matchups: {e}")

    emit(update_text("loading_items", lang))
    known_items = []
    items = []
    pools = []
    ddragon_version = ""
    try:
        ddragon_version = fetch_ddragon_version(net)
        _check_cancel(cancel_check)
        pc_ru = fetch_ddragon_item_ru_map(net, ddragon_version)
        _check_cancel(cancel_check)
    except UpdateCancelled:
        raise
    except Exception as e:
        pc_ru = {}
        summary["errors"].append(f"Item localization: {e}")
    try:
        # Build Trends determine which final items suit each champion, while the
        # catalog itself is the complete WR Pocket shop.  Keeping these separate
        # lets counter-items use every current final item without allowing recipe
        # components into a recommended build.
        pools = fetch_wrpocket_item_pools(net, resolve, emit)
        _check_cancel(cancel_check)
        wanted_items = {canonical_item_name(row[1]) for row in pools if clean_item_name(row[1])}
        headers = _item_dataset_headers()
        force_item_schema_refresh = db.get_meta("item_dataset_schema_version", "") != ITEM_DATA_SCHEMA_VERSION
        dataset = fetch_wrpocket_item_dataset(net, None, headers)
        _check_cancel(cancel_check)

        # A 304 is reusable only when this installation already has both the
        # catalog rows and characteristics.  Users upgrading from an older DB
        # may have item names/icons but zero tooltip stats, so force one body
        # download in that case.
        existing_by_slug = {slugish(clean_item_name(row.get("name", ""))): row for row in db.item_catalog_rows()}
        if dataset.status_code == 304:
            items = _catalog_finished_items()
            missing_details = item_detail_fallback_names(
                wanted_items, [], existing_by_slug, current_patch, force_refresh=force_item_schema_refresh,
            )
            covered = {slugish(x[0]) for x in items}
            wanted_slugs = {slugish(x) for x in wanted_items}
            if wanted_slugs - covered or missing_details:
                dataset = fetch_wrpocket_item_dataset(net, None, {})
                _check_cancel(cancel_check)

        parsed_rows = list(dataset.rows or []) if dataset.status_code != 304 else []
        if dataset.status_code != 304:
            detail_rows: list[dict] = []
            # Apply every valid catalog row we could parse.  Do not reject the
            # entire dataset merely because WR Pocket changed one part of the
            # page: missing items are recovered from their detail pages below.
            if parsed_rows:
                parsed_items, changed = _apply_item_dataset(parsed_rows, current_patch, pc_ru)
                summary["item_details_changed"] += changed
                if parsed_items:
                    items = parsed_items
                    if not _replace_finished_catalog(parsed_rows, pc_ru):
                        summary["errors"].append("WR Pocket item catalog parse was incomplete; previous final-item catalog was kept.")
            if dataset.dataset_hash:
                db.set_meta("item_dataset_hash", dataset.dataset_hash)
            if dataset.etag:
                db.set_meta("item_dataset_etag", dataset.etag)
            if dataset.last_modified:
                db.set_meta("item_dataset_last_modified", dataset.last_modified)

            existing_by_slug = {slugish(clean_item_name(row.get("name", ""))): row for row in db.item_catalog_rows()}
            missing_details = item_detail_fallback_names(
                wanted_items, parsed_rows, existing_by_slug, current_patch,
                force_refresh=force_item_schema_refresh,
            )
            if missing_details:
                detail_rows = fetch_wrpocket_item_detail_dataset(net, missing_details, emit)
                _check_cancel(cancel_check)
                if detail_rows:
                    _detail_items, detail_changed = _apply_item_dataset(detail_rows, current_patch, pc_ru)
                    summary["item_details_changed"] += detail_changed

            refreshed_rows = parsed_rows + detail_rows
            if any(row.get("stats") or row.get("effect_en") for row in refreshed_rows):
                db.set_meta("item_dataset_schema_version", ITEM_DATA_SCHEMA_VERSION)

        db.set_meta("item_dataset_checked_patch", current_patch or previous_patch)

        # Re-read the complete final-item catalog after the index + detail
        # fallback.  Unknown tiers are deliberately excluded rather than guessed.
        items = _catalog_finished_items()

        pools = _canonicalize_item_pools(pools, items)
        pools = _filter_finished_item_pools(pools, {row[0] for row in items})
        db.replace_source_item_pools("wrpocket.app", pools)
        known_items = [x[0] for x in items]
        summary["item_pool"] = len(pools)
        missing_ru = []
        for name in known_items:
            row = db.get_item(name) or {}
            if not (row.get("name_ru") or ""):
                missing_ru.append(name)
        if missing_ru:
            summary["errors"].append("RU item names missing: " + ", ".join(missing_ru[:12]) + ("…" if len(missing_ru) > 12 else ""))
    except UpdateCancelled:
        raise
    except Exception as e:
        summary["errors"].append(f"Wild Rift Pocket: {e}")
        known_items = db.get_item_names()

    emit(update_text("loading_counter_items", lang))
    try:
        counter_items = fetch_counter_item_pages(net, resolve, known_items, emit)
        _check_cancel(cancel_check)
        db.replace_source_counter_items("wildriftcounter.com", counter_items)
        summary["counter_items"] = len(counter_items)
    except UpdateCancelled:
        raise
    except Exception as e:
        summary["errors"].append(f"WildRiftCounter items: {e}")

    emit(update_text("caching_media", lang))
    if not ddragon_version:
        try:
            ddragon_version = fetch_ddragon_version(net)
        except Exception:
            ddragon_version = ""
    if not items:
        items = [(row["name"], row.get("name_ru", ""), row.get("icon_url", ""))
                 for row in db.item_catalog_rows()]
    ci, ii = _cache_media(
        net, champs, items, ddragon_version, emit, lang,
        current_patch=current_patch, previous_patch=previous_patch, errors=summary["errors"],
        cancel_check=cancel_check,
    )
    summary["champion_images"] = ci
    summary["item_images"] = ii

    _check_cancel(cancel_check)
    stamp = format_update_timestamp(now)
    db.set_meta("last_update", stamp)
    db.set_meta("last_update_iso", now_iso)
    db.set_meta("source_note", "Champions/stats: ry2x; tiers/matchups: WildRiftCore; counter-items: WildRiftCounter; item catalog/trends/media: Wild Rift Pocket; RU shared item names: Riot Data Dragon + Wild Rift overrides")
    raw_progress(update_text("done", lang))
    return summary
