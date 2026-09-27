from __future__ import annotations

from datetime import datetime
import re
import time
from pathlib import Path
from typing import Callable

import db
from localization import COMMON_CHAMPION_ALIASES, champion_name_ru
from media_cache import BRAND_DIR, CHAMPION_DIR, ITEM_DIR, cache_brand_logo, ensure_cache_dirs, safe_name, sync_cached_image, _valid_image
from sources import (
    DDRAGON_CHAMPION_ICON, Net, fetch_champions_locale, fetch_wildriftmeta_champion_roster, fetch_counter_item_pages,
    fetch_ddragon_item_ru_map, fetch_ddragon_version, fetch_stats, fetch_wrpocket_item_pools,
    fetch_wrpocket_item_dataset, fetch_wrpocket_item_detail_dataset,
    item_detail_fallback_names, fetch_current_patch_info, item_name_ru,
    parse_wildriftcore_matchups, parse_wildriftcore_tiers, fetch_wildriftcore_role_builds,
    fetch_wildriftcore_item_metadata,
    slugish, clean_item_name, clean_wrpocket_item_stats,
    clean_wrpocket_item_effect, _item_dataset_hash, canonical_item_name, is_finished_item_tier,
    trusted_item_icon_urls, fetch_verified_item_icon_urls, is_trusted_item_icon_url, is_dns_resolution_error,
)


ITEM_DATA_SCHEMA_VERSION = "4"
ITEM_ICON_SCHEMA_VERSION = "7"


class UpdateCancelled(RuntimeError):
    """Raised when a cooperative update cancellation is requested."""


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise UpdateCancelled("Update cancelled")


UPDATE_TEXT = {
    "ru": {
        "loading_champions": "1/8 Загружаю чемпионов EN/RU…",
        "loading_stats": "2/8 Загружаю актуальные win/pick/ban…",
        "loading_tiers": "3/8 Загружаю тиры чемпионов WildRiftCore…",
        "loading_matchups": "4/8 Загружаю матрицу контрпиков…",
        "loading_items": "5/8 Загружаю предметы и русские названия…",
        "loading_role_builds": "6/8 Загружаю сборки по чемпиону и роли…",
        "loading_counter_items": "7/8 Загружаю сигналы контрмер…",
        "caching_media": "8/8 Кэширую портреты и иконки…",
        "cache_champions": "Кэш портретов: {current}/{total}",
        "cache_items": "Кэш предметов: {current}/{total}",
        "done": "Обновление завершено.",
    },
    "en": {
        "loading_champions": "1/8 Loading champions EN/RU…",
        "loading_stats": "2/8 Loading current win/pick/ban…",
        "loading_tiers": "3/8 Loading WildRiftCore champion tiers…",
        "loading_matchups": "4/8 Loading counter-pick matrix…",
        "loading_items": "5/8 Loading items and localized names…",
        "loading_role_builds": "6/8 Loading champion+role builds…",
        "loading_counter_items": "7/8 Loading countermeasure signals…",
        "caching_media": "8/8 Caching champion portraits and item icons…",
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
        merged["name_ru"] = (
            (ru_by_id.get(row["id"]) or {}).get("name", "")
            or champion_name_ru(str(row.get("id") or ""), str(row.get("name") or ""))
        )
        out.append(merged)
    return out


def merge_champion_roster_supplement(
    champions: list[dict], roster_rows: list[dict],
) -> list[dict]:
    """Add genuinely new WR identities without duplicating known champions.

    Supplemental profile slugs can spell an existing champion differently
    ("Nunu And Willump" vs canonical id "Nunu"). Treat all known aliases as
    identity keys before deciding that a roster row is new.
    """
    out = [dict(row) for row in champions]
    known: dict[str, str] = {}

    def register(champ: dict) -> None:
        cid = str(champ.get("id") or "")
        values = [
            cid,
            str(champ.get("name") or ""),
            str(champ.get("name_ru") or ""),
            *COMMON_CHAMPION_ALIASES.get(cid, ()),
        ]
        for value in values:
            key = slugish(value)
            if key:
                known.setdefault(key, cid)

    for champ in out:
        register(champ)

    for raw in roster_rows:
        row = dict(raw)
        identity_values = (
            str(row.get("id") or ""),
            str(row.get("name") or ""),
            str(row.get("profile_slug") or "").replace("-", " "),
        )
        if any(slugish(value) in known for value in identity_values if slugish(value)):
            continue
        row["name_ru"] = (
            str(row.get("name_ru") or "")
            or champion_name_ru(str(row.get("id") or ""), str(row.get("name") or ""))
        )
        out.append(row)
        register(row)

    return out


def build_resolver(champs: list[dict]):
    table: dict[str, str] = {}
    # Profile URLs must be resolved against champion IDs only. Display names can
    # temporarily be wrong/stale in an upstream locale feed; allowing those names
    # to define URL identity is exactly how /champions/norra could resolve to Zyra.
    id_table: dict[str, str] = {}
    manual = {
        "khazix": "Kha'Zix", "kaisa": "Kai'Sa", "kogmaw": "Kog'Maw", "chogath": "Cho'Gath",
        "drmundo": "DrMundo", "nunuandwillump": "Nunu", "nunuwillump": "Nunu",
        "jarvaniv": "JarvanIV", "monkeyking": "Wukong", "wukong": "MonkeyKing",
    }
    for champ in champs:
        cid = str(champ["id"])
        cid_key = slugish(cid)
        if cid_key:
            id_table[cid_key] = cid
        for value in (cid, champ.get("name", ""), champ.get("name_ru", "")):
            key = slugish(value or "")
            if key:
                # Do not let a later locale/display-name collision silently
                # overwrite an already known identity.
                table.setdefault(key, cid)

    wanted_by_key = {slugish(str(champ["id"])): str(champ["id"]) for champ in champs}
    for alias, wanted in manual.items():
        wanted_cid = wanted_by_key.get(slugish(wanted))
        if wanted_cid:
            table.setdefault(alias, wanted_cid)
            id_table.setdefault(alias, wanted_cid)

    def resolve_exact_id(value: str):
        key = slugish(value)
        return id_table.get(key) if key else None

    def resolve(value: str):
        key = slugish(value)
        if not key:
            return None
        if key in table:
            return table[key]
        candidates = [cid for k, cid in table.items() if k.startswith(key) or key.startswith(k)]
        return candidates[0] if len(set(candidates)) == 1 else None

    # Scrapers that already possess a canonical /champions/<slug> URL must use
    # this strict resolver rather than fuzzy/display-name matching.
    resolve.exact_id = resolve_exact_id
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
    # Migration path for users upgrading from the pre-manifest cache. Trust the
    # old file only when SQLite still points to a cached path. If icon_path was
    # deliberately cleared because the verified URL changed, a leftover PNG on
    # disk is stale and must not be "re-blessed" as if it belonged to the new
    # URL on the next update.
    stored_path = str((existing_row or {}).get("icon_path") or "").strip()
    if target.exists() and stored_path:
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


def _replace_finished_catalog(
    records: list[dict], pc_ru: dict[str, str], preserve_names: set[str] | None = None,
) -> bool:
    """Refresh the finished catalog without deleting source-referenced items.

    The catalog index and champion Build Trends are separate pages. If the index
    parser temporarily misses an item that current champion pages still reference
    (notably a boot), preserve the last known-good finished catalog row until the
    detail fallback can repair it.
    """
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

    preserve_slugs = {slugish(canonical_item_name(name)) for name in (preserve_names or set()) if canonical_item_name(name)}
    if preserve_slugs:
        for item in db.item_catalog_rows():
            name = canonical_item_name(item.get("name", ""))
            key = slugish(name)
            if not name or key in seen or key not in preserve_slugs:
                continue
            if not is_finished_item_tier(str(item.get("tier") or "")):
                continue
            seen.add(key)
            rows.append((
                name, str(item.get("category") or ""),
                str(item.get("name_ru") or item_name_ru(name, pc_ru) or ""),
                str(item.get("icon_url") or ""), "Upgraded",
            ))

    # WR currently has far more than fifty final items. If a layout change makes
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


def _collapse_dns_warnings(errors: list[str], net: Net) -> list[str]:
    """Collapse one device DNS outage into one actionable warning.

    The detailed per-source failures all describe the same resolver problem and
    otherwise make the Android UI look as if four independent parsers broke.
    """
    if not getattr(net, "dns_outage", False):
        return errors
    dns_entries = [str(value) for value in errors if is_dns_resolution_error(value)]
    media_dns_entries = [
        str(value) for value in errors
        if str(value).startswith("Media refresh skipped: DNS")
    ]
    if not dns_entries:
        return errors

    affected: list[str] = []
    for value in dns_entries:
        prefix = value.split(":", 1)[0].strip()
        if prefix and prefix not in affected:
            affected.append(prefix)
    hosts = list(getattr(net, "dns_failed_hosts", ()) or ())
    message = (
        "Network/DNS: Android не смог разрешить имена "
        + (", ".join(hosts) if hosts else "нескольких источников")
        + ". Предыдущие данные сохранены."
    )
    if affected:
        message += "\nЗатронуто: " + ", ".join(affected)
    for value in media_dns_entries:
        message += "\n" + value

    return [message] + [
        str(value) for value in errors
        if not is_dns_resolution_error(value)
        and not str(value).startswith("Media refresh skipped: DNS")
    ]


def _cache_media(
    net: Net, champs: list[dict], items: list[tuple], version: str, progress: Callable[[str], None],
    lang: str = "ru", current_patch: str = "", previous_patch: str = "",
    errors: list[str] | None = None, cancel_check: Callable[[], bool] | None = None,
    force_item_refresh: bool = False,
) -> tuple[int, int, int]:
    ensure_cache_dirs()
    champ_count = 0
    item_count = 0
    item_failures = 0
    errors = errors if errors is not None else []
    # One unavailable CDN/source must not become 200+ user-facing warnings.
    # Keep every per-asset detail for the log, but aggregate them into one
    # warning per media class for the update summary.
    champion_media_issues: list[str] = []
    item_media_issues: list[str] = []

    # If two unrelated source domains already failed DNS earlier in this same
    # update, do not turn that one resolver outage into 100+ image refresh
    # attempts. Keep every existing cache file untouched and leave a pending
    # icon-schema migration pending for the next successful network run.
    if getattr(net, "dns_outage", False):
        cached_champs = sum(
            1
            for champ in champs
            if (CHAMPION_DIR / f"{safe_name(champ['id'])}.png").is_file()
        )
        cached_items = sum(
            1
            for row in items
            if row and (ITEM_DIR / f"{safe_name(canonical_item_name(str(row[0] or '')))}.png").is_file()
        )
        errors.append(
            "Media refresh skipped: DNS недоступен; "
            f"сохранён локальный кэш чемпионов {cached_champs}/{len(champs)}, "
            f"предметов {cached_items}/{len(items)}."
        )
        progress(update_text("cache_champions", lang, current=len(champs), total=len(champs)))
        progress(update_text("cache_items", lang, current=len(items), total=len(items)))
        # A forced icon migration must not be marked complete while offline.
        return cached_champs, cached_items, (1 if force_item_refresh else 0)

    progress(update_text("cache_champions", lang, current=0, total=len(champs)))
    for idx, c in enumerate(champs, 1):
        _check_cancel(cancel_check)
        target = CHAMPION_DIR / f"{safe_name(c['id'])}.png"
        existing = db.champion_by_name_or_id(c["id"])
        candidates: list[str] = []
        if version:
            candidates.append(DDRAGON_CHAMPION_ICON.format(version=version, champion_id=c["id"]))
        stored_url = str((existing or {}).get("icon_url") or c.get("icon_url") or "")
        if stored_url and stored_url not in candidates:
            candidates.append(stored_url)
        # Generic WildRiftMeta fallback for WR-exclusive champions absent from
        # PC Data Dragon. Prefer the exact profile URL-derived icon when the
        # roster supplement provided one.
        if not stored_url:
            slug = re.sub(r"[^a-z0-9]+", "-", str(c.get("name") or c["id"]).casefold()).strip("-")
            if slug:
                candidates.append(
                    f"https://www.wildriftmeta.com/assets/champion/icon/champion-{slug}-icon.png"
                )

        asset_key = f"champion:{c['id']}"
        chosen = None
        last_status = "failed"
        for url in candidates:
            record = _seed_media_record(asset_key, url, existing, target, previous_patch, current_patch)
            try:
                result = sync_cached_image(
                    net, url, target, record,
                    current_patch=current_patch, previous_patch=previous_patch,
                    force_refresh=False,
                )
            except Exception as exc:
                last_status = str(exc)
                continue
            last_status = result.status
            if result.status == "stale_kept" and result.path:
                # The local portrait is still valid; a failed conditional refresh
                # is not a broken image. Keep it without turning one network
                # interruption into a warning for every champion.
                chosen = (str(result.source_url or url), result)
                break
            if result.status in {"failed", "missing_url"}:
                continue
            if result.path:
                chosen = (url, result)
                break

        if chosen is None:
            champion_media_issues.append(f"Portrait {c['id']}: {last_status}")
        else:
            url, result = chosen
            _store_media_result(asset_key, result)
            db.update_champion_media(c["id"], result.source_url or url, _portable_path(result.path))
            champ_count += 1
        progress(update_text("cache_champions", lang, current=idx, total=len(champs)))

    progress(update_text("cache_items", lang, current=0, total=len(items)))
    for idx, row in enumerate(items, 1):
        _check_cancel(cancel_check)
        name = canonical_item_name(str(row[0] or ""))
        if not name:
            continue
        target = ITEM_DIR / f"{safe_name(name)}.png"
        existing = db.get_item(name)
        asset_key = f"item:{name}"

        # Final item art must come from a trusted WR asset source tied to this
        # exact item. Preserve an already verified source URL first: many seed
        # icons use Riot/Tencent EquipIcons and should not be blanked merely
        # because a newer mirror is temporarily unavailable.
        generated_candidates = trusted_item_icon_urls(name)
        manifest = db.get_media_asset(asset_key)
        manifest_url = str((manifest or {}).get("source_url") or "")
        existing_url = str((existing or {}).get("icon_url") or "")

        candidates: list[str] = []
        for icon_url in (existing_url, manifest_url, *generated_candidates):
            if (
                icon_url
                and is_trusted_item_icon_url(icon_url)
                and icon_url not in candidates
            ):
                candidates.append(icon_url)

        chosen = None
        last_status = "failed"
        last_exc = ""

        def try_icon_candidates(urls: list[str], start_index: int = 0):
            nonlocal chosen, last_status, last_exc
            for offset, icon_url in enumerate(urls):
                candidate_index = start_index + offset
                last_icon_request = float(getattr(net, "_item_icon_last_request", 0.0))
                elapsed = time.monotonic() - last_icon_request
                if elapsed < 0.35:
                    time.sleep(0.35 - elapsed)
                record = _seed_media_record(
                    asset_key, icon_url, existing, target, previous_patch, current_patch
                )
                record_url = str((record or {}).get("source_url") or "")
                existing_path = str((existing or {}).get("icon_path") or "")
                force_this_item = bool(
                    force_item_refresh
                    or candidate_index > 0
                    or not existing_path
                    or (record_url and record_url != icon_url)
                    or not (record or {}).get("sha256")
                )
                try:
                    result = sync_cached_image(
                        net, icon_url, target, record,
                        current_patch=current_patch, previous_patch=previous_patch,
                        force_refresh=force_this_item,
                    )
                    net._item_icon_last_request = time.monotonic()
                except Exception as exc:
                    net._item_icon_last_request = time.monotonic()
                    last_exc = str(exc)
                    last_status = "exception"
                    continue

                last_status = result.status

                # A previously verified file from this exact trusted URL is still
                # valid when only the revalidation request failed. This is the
                # legitimate Immortal Boots stale_kept case from the device log.
                if (
                    result.status == "stale_kept"
                    and result.path
                    and record_url == icon_url
                    and is_trusted_item_icon_url(icon_url)
                ):
                    chosen = (icon_url, result)
                    return

                if result.status in {"failed", "stale_kept", "missing_url"}:
                    continue
                if result.path:
                    chosen = (icon_url, result)
                    return

        if candidates:
            try_icon_candidates(candidates)

        if chosen is None:
            page_candidates = [
                url for url in fetch_verified_item_icon_urls(net, name, progress)
                if url not in candidates
            ]
            if page_candidates:
                try_icon_candidates(page_candidates, start_index=len(candidates))

        if chosen is None:
            item_failures += 1
            detail = last_exc or last_status
            item_media_issues.append(f"Item image {name}: {detail}")
            # Never keep a file that belongs to another/unverified source under
            # the requested item name.
            db.clear_item_icon_path(name)
            db.delete_media_asset(asset_key)
        else:
            icon_url, result = chosen
            _store_media_result(asset_key, result)
            db.update_item_media(name, icon_url, _portable_path(result.path))
            item_count += 1

        progress(update_text("cache_items", lang, current=idx, total=len(items)))

    if champion_media_issues:
        errors.append(
            f"Champion media cache: {len(champion_media_issues)} проблем\n"
            + "\n".join(champion_media_issues)
        )
    if item_media_issues:
        errors.append(
            f"Item media cache: {len(item_media_issues)} проблем\n"
            + "\n".join(item_media_issues)
        )

    _check_cancel(cancel_check)
    try:
        logo_path = cache_brand_logo(net)
        if logo_path:
            db.set_meta("brand_logo_path", _portable_path(logo_path))
    except Exception:
        pass
    return champ_count, item_count, item_failures


def _audit_item_icon_integrity() -> tuple[list[str], list[str]]:
    """Return (referenced items missing from catalog, catalog/ref items without valid art)."""
    from paths import resolve_media_path

    snapshot = db.load_runtime_snapshot()
    items: dict[str, dict] = snapshot.get("items", {}) or {}
    referenced: set[str] = set()

    for rows in (snapshot.get("item_pools", {}) or {}).values():
        referenced.update(
            canonical_item_name(str(row.get("item_name") or ""))
            for row in rows
            if canonical_item_name(str(row.get("item_name") or ""))
        )
    for rows in (snapshot.get("counter_items", {}) or {}).values():
        referenced.update(
            canonical_item_name(str(row.get("item_name") or ""))
            for row in rows
            if canonical_item_name(str(row.get("item_name") or ""))
        )
    for row in (snapshot.get("role_builds", {}) or {}).values():
        referenced.update(
            canonical_item_name(str(name or ""))
            for name in (row.get("items") or [])
            if canonical_item_name(str(name or ""))
        )
        boot = canonical_item_name(str(row.get("boot_name") or ""))
        if boot:
            referenced.add(boot)
    for rows in (snapshot.get("role_variants", {}) or {}).values():
        for row in rows:
            referenced.update(
                canonical_item_name(str(name or ""))
                for name in (row.get("items") or [])
                if canonical_item_name(str(name or ""))
            )
    for mapping_name in (
        "role_situational",
        "role_boots",
        "role_opponent_adaptations",
    ):
        for rows in (snapshot.get(mapping_name, {}) or {}).values():
            referenced.update(
                canonical_item_name(str(row.get("item_name") or ""))
                for row in rows
                if canonical_item_name(str(row.get("item_name") or ""))
            )

    catalog_names = set(items)
    missing_catalog = sorted(name for name in referenced if name not in catalog_names)

    # Audit every final item, plus anything a current build can render.
    names_to_check = {
        name
        for name, row in items.items()
        if is_finished_item_tier(str(row.get("tier") or ""))
    } | (referenced & catalog_names)

    missing_icons: list[str] = []
    for name in sorted(names_to_check):
        row = items.get(name) or {}
        icon_path = str(row.get("icon_path") or "").strip()
        if not icon_path:
            missing_icons.append(name)
            continue
        try:
            resolved = resolve_media_path(icon_path)
        except Exception:
            missing_icons.append(name)
            continue
        if not _valid_image(resolved):
            missing_icons.append(name)

    return missing_catalog, missing_icons


def _audit_wrc_build_integrity() -> dict:
    """Audit source blocks that directly control item recommendations."""
    snapshot = db.load_runtime_snapshot()
    champions = snapshot.get("champions", []) or []
    champion_ids = {str(row.get("id") or "") for row in champions if row.get("id")}
    role_builds = snapshot.get("role_builds", {}) or {}
    role_variants = snapshot.get("role_variants", {}) or {}
    role_situational = snapshot.get("role_situational", {}) or {}
    role_opponents = snapshot.get("role_opponent_adaptations", {}) or {}

    build_champions = {cid for cid, _role in role_builds}
    missing_champions = sorted(champion_ids - build_champions)
    missing_variant_roles = sorted(
        f"{cid}:{role}"
        for cid, role in set(role_builds) - set(role_variants)
    )
    incomplete_variant_sets = sorted(
        f"{cid}:{role}={len(role_variants.get((cid, role), []))}/3"
        for cid, role in role_builds
        if len(role_variants.get((cid, role), [])) < 3
    )
    missing_situational_roles = sorted(
        f"{cid}:{role}"
        for cid, role in role_builds
        if not role_situational.get((cid, role))
    )
    missing_opponent_adaptation_roles = sorted(
        f"{cid}:{role}"
        for cid, role in role_builds
        if not role_opponents.get((cid, role))
    )

    incomplete_builds: list[str] = []
    for (cid, role), row in role_builds.items():
        items = [str(x) for x in (row.get("items") or []) if str(x).strip()]
        boot = str(row.get("boot_name") or "").strip()
        if len(items) != 5 or not boot:
            incomplete_builds.append(f"{cid}:{role}")

    incomplete_variants: list[str] = []
    for (cid, role), rows in role_variants.items():
        for row in rows:
            items = [str(x) for x in (row.get("items") or []) if str(x).strip()]
            trigger = str(row.get("trigger_text") or "").strip()
            examples = [
                str(x).strip()
                for x in (row.get("example_enemies") or [])
                if str(x).strip()
            ]
            if len(items) != 5 or not trigger or not examples:
                incomplete_variants.append(
                    f"{cid}:{role}:{row.get('variant_name') or '?'}"
                )

    return {
        "champions_total": len(champion_ids),
        "role_builds": len(role_builds),
        "variants": sum(len(rows) for rows in role_variants.values()),
        "missing_champions": missing_champions,
        "missing_variant_roles": missing_variant_roles,
        "incomplete_variant_sets": incomplete_variant_sets,
        "missing_situational_roles": missing_situational_roles,
        "missing_opponent_adaptation_roles": missing_opponent_adaptation_roles,
        "opponent_adaptations": sum(
            len(rows) for rows in role_opponents.values()
        ),
        "incomplete_builds": incomplete_builds,
        "incomplete_variants": incomplete_variants,
    }


def update_all(
    progress: Callable[[str], None] | None = None,
    lang: str = "ru",
    cancel_check: Callable[[], bool] | None = None,
) -> dict:
    _check_cancel(cancel_check)
    db.init_db()
    # Repair obsolete source shorthands before any catalog/build/media work.
    # This is network-independent, so an existing "Mercury Boots" row is folded
    # into the real patch item even if the online sources are unavailable.
    db.migrate_item_aliases({
        "Mercury Boots": "Mercury's Treads",
        "Mercury Treads": "Mercury's Treads",
        "Mercurys Treads": "Mercury's Treads",
    })
    raw_progress = progress or (lambda s: None)

    def emit(message: str) -> None:
        _check_cancel(cancel_check)
        raw_progress(message)

    net = Net()
    summary = {
        "champions": 0, "stats": 0, "tiers": 0, "matchups": 0,
        "item_pool": 0, "role_builds": 0, "role_build_variants": 0,
        "wrc_build_profiles_total": 0, "wrc_build_pages_success": 0,
        "wrc_build_failed_profiles": [], "wrc_build_integrity": {},
        "counter_items": 0,
        "champion_images": 0, "item_images": 0, "item_details_changed": 0,
        "patch": "", "errors": [],
    }
    previous_patch = db.get_meta("patch_version", "")
    current_patch = previous_patch
    force_item_icon_refresh = db.get_meta("item_icon_schema_version", "") != ITEM_ICON_SCHEMA_VERSION

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

    # Supplement only genuinely missing identities from the current live roster.
    # The structured ry2x metadata remains authoritative for all champions it
    # already knows. This currently recovers Norra, whose WR profile exists in
    # patch 7.3 while the merged feed can lag behind.
    try:
        roster_rows = fetch_wildriftmeta_champion_roster(net)
        champs = merge_champion_roster_supplement(champs, roster_rows)
    except Exception as e:
        # Roster supplement is non-destructive; the primary structured feed is
        # still usable when the supplementary site is temporarily unavailable.
        summary["errors"].append(f"Champion roster supplement: {e}")

    now = datetime.now().astimezone()
    now_iso = now.isoformat()
    for c in champs:
        source = "wildriftmeta.com:roster" if c.get("profile_slug") else "ry2x/WildRift-Merged-Champion-Data"
        db.upsert_champion(
            c["id"], c["name"], c["roles"], c["lanes"], c["damage_type"],
            source, now_iso, name_ru=c.get("name_ru", ""),
            icon_url=c.get("icon_url", ""),
        )
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
        # Icon schema changes require a real catalog body even when the server
        # would normally answer 304. We need each item's detail URL so the exact
        # icon can be re-verified against its own page.
        headers = {} if force_item_icon_refresh else _item_dataset_headers()
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
        if parsed_rows:
            # WR Pocket remains the data/stat/build-trend source, but no longer
            # has authority over item artwork. Current WR Pocket detail pages can
            # expose generic or PC artwork under a valid WR item name (confirmed
            # for Kaenic Rookern and Sundered Sky). Use name-addressed WR icon
            # sources instead; the media layer validates the downloaded bytes.
            for row in parsed_rows:
                name = canonical_item_name(str(row.get("name") or ""))
                if not name or not is_finished_item_tier(str(row.get("tier") or "")):
                    continue
                urls = trusted_item_icon_urls(name)
                row["icon_url"] = urls[0] if urls else ""
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
                    if not _replace_finished_catalog(parsed_rows, pc_ru, wanted_items):
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
                    for row in detail_rows:
                        name = canonical_item_name(str(row.get("name") or ""))
                        urls = trusted_item_icon_urls(name) if name else []
                        row["icon_url"] = urls[0] if urls else ""
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

    emit(update_text("loading_role_builds", lang))
    try:
        (
            role_builds, role_situational, role_boots,
            role_opponent_adaptations, role_variants,
            role_build_errors, role_build_coverage,
        ) = fetch_wildriftcore_role_builds(net, resolve, known_items, emit)
        _check_cancel(cancel_check)
        db.replace_source_role_builds_partial(
            "wildriftcore.com",
            role_builds,
            role_situational,
            role_boots,
            variants=role_variants,
            opponent_adaptations=role_opponent_adaptations,
        )
        summary["role_builds"] = len(role_builds)
        summary["role_build_variants"] = len(role_variants)
        summary["role_opponent_adaptations"] = len(role_opponent_adaptations)
        lane_updates = db.merge_champion_lanes_from_role_builds("wildriftcore.com")
        summary["wrc_lane_updates"] = int(lane_updates)
        summary["wrc_build_profiles_total"] = int(
            role_build_coverage.get("profiles_total") or 0
        )
        summary["wrc_build_pages_success"] = int(
            role_build_coverage.get("pages_success") or 0
        )
        summary["wrc_build_failed_profiles"] = list(
            role_build_coverage.get("failed_profiles") or []
        )
        db.set_meta(
            "wrc_build_profiles_total",
            str(summary["wrc_build_profiles_total"]),
        )
        db.set_meta(
            "wrc_build_pages_success",
            str(summary["wrc_build_pages_success"]),
        )
        db.set_meta("wrc_build_roles_total", str(len(role_builds)))
        db.set_meta("wrc_build_variants_total", str(len(role_variants)))
        db.set_meta(
            "wrc_build_opponent_adaptations_total",
            str(len(role_opponent_adaptations)),
        )

        # WildRiftCore can publish a current role item before WR Pocket exposes
        # it in the local catalog (support gold/new patch items are common).
        # Fetch only those missing names from their own WRC item pages so the
        # source-approved build remains complete and keeps a verified icon.
        role_item_names: set[str] = set()
        source_boot_names: set[str] = set()
        for _cid, _role, build_items, boot_name, _patch, _url in role_builds:
            role_item_names.update(
                canonical_item_name(x)
                for x in build_items if canonical_item_name(x)
            )
            boot = canonical_item_name(boot_name)
            if boot:
                role_item_names.add(boot)
                source_boot_names.add(boot)
        role_item_names.update(
            canonical_item_name(row[2])
            for row in role_situational if canonical_item_name(row[2])
        )
        role_item_names.update(
            canonical_item_name(row[3])
            for row in role_opponent_adaptations
            if canonical_item_name(row[3])
        )
        for raw_variant in role_variants:
            variant_items = raw_variant[3] if len(raw_variant) > 3 else []
            role_item_names.update(
                canonical_item_name(x)
                for x in variant_items if canonical_item_name(x)
            )
        for row in role_boots:
            boot = canonical_item_name(row[2])
            if boot:
                role_item_names.add(boot)
                source_boot_names.add(boot)

        # The current role-build source is authoritative that these are usable
        # boots in the current patch. Older WR Pocket catalog snapshots may mark
        # names such as Mercury's Treads / Plated Steelcaps as intermediate.
        # Promote only source-declared boot slots, never arbitrary catalog items.
        for boot in sorted(source_boot_names):
            urls = trusted_item_icon_urls(boot)
            db.upsert_item(
                boot, "Boots", "wildriftcore.com",
                name_ru=item_name_ru(boot, pc_ru),
                icon_url=(urls[0] if urls else ""),
                tier="Upgraded",
            )
        existing_names = {canonical_item_name(name) for name in db.get_item_names()}
        missing_role_items = sorted(name for name in role_item_names if name and name not in existing_names)
        if missing_role_items:
            # The exact champion+role build page is already sufficient authority
            # that these names are recommendable role items. Seed them first so
            # a temporary metadata/icon request failure never deletes a valid
            # source-approved slot; the request below enriches the record.
            for name in missing_role_items:
                db.upsert_item(
                    name, "", "wildriftcore.com",
                    name_ru=item_name_ru(name, pc_ru), tier="Upgraded",
                )
            source_item_rows, source_item_errors = fetch_wildriftcore_item_metadata(
                net, missing_role_items, emit
            )
            _check_cancel(cancel_check)
            for row in source_item_rows:
                name = canonical_item_name(row.get("name", ""))
                if not name:
                    continue
                ru_name = item_name_ru(name, pc_ru)
                trusted_urls = trusted_item_icon_urls(name)
                page_icon_url = str(row.get("icon_url") or "")
                preferred_icon_url = (
                    page_icon_url
                    if is_trusted_item_icon_url(page_icon_url)
                    else (trusted_urls[0] if trusted_urls else "")
                )
                # parse_wildriftcore_item_page already matched the image to this
                # exact item name. Do not throw that verified src away and
                # replace it with a guessed CDN path.
                db.upsert_item(
                    name, str(row.get("category") or ""), "wildriftcore.com",
                    name_ru=ru_name,
                    icon_url=preferred_icon_url,
                    tier="Upgraded",
                )
                db.upsert_item_details(
                    name, price=int(row.get("price") or 0),
                    stats=list(row.get("stats") or []), effect_en=str(row.get("effect_en") or ""),
                    data_hash=_item_dataset_hash(
                        name, int(row.get("price") or 0), list(row.get("stats") or []),
                        str(row.get("effect_en") or ""),
                        preferred_icon_url,
                        str(row.get("detail_url") or ""),
                    ),
                    data_patch=current_patch, source_url=str(row.get("detail_url") or ""),
                )
            if source_item_errors:
                detail = "; ".join(source_item_errors[:3])
                summary["errors"].append(
                    f"WildRiftCore item metadata: {len(source_item_errors)} не загружено"
                    + (f" ({detail})" if detail else "")
                )
            items = _catalog_finished_items()
            known_items = [x[0] for x in items]

        # Re-read after role-source enrichment/promotions so newly current boots
        # (for example Mercury's Treads) are included in media caching and
        # counter-item matching immediately in this same update.
        items = _catalog_finished_items()
        known_items = [x[0] for x in items]

        if role_build_errors:
            detail = "; ".join(role_build_errors[:3])
            summary["errors"].append(
                f"WildRiftCore builds: {len(role_build_errors)} страниц не обновлены; "
                f"покрытие {summary['wrc_build_pages_success']}/"
                f"{summary['wrc_build_profiles_total']}; сохранены предыдущие данные"
                + (f" ({detail})" if detail else "")
            )
    except UpdateCancelled:
        raise
    except Exception as e:
        # Build data is patch-cached and role rows are replaced only partially,
        # so a temporary source failure must never erase the last known-good core.
        summary["errors"].append(f"WildRiftCore builds: {e}")

    build_integrity = _audit_wrc_build_integrity()
    summary["wrc_build_integrity"] = build_integrity
    build_gap_parts: list[str] = []
    if build_integrity.get("missing_champions"):
        names = list(build_integrity["missing_champions"])
        build_gap_parts.append(
            f"без сборок {len(names)}: " + ", ".join(names[:12])
            + ("…" if len(names) > 12 else "")
        )
    if build_integrity.get("missing_variant_roles"):
        names = list(build_integrity["missing_variant_roles"])
        build_gap_parts.append(
            f"без вариантов {len(names)}: " + ", ".join(names[:8])
            + ("…" if len(names) > 8 else "")
        )
    if build_integrity.get("incomplete_variant_sets"):
        names = list(build_integrity["incomplete_variant_sets"])
        build_gap_parts.append(
            f"неполный набор вариантов {len(names)}: " + ", ".join(names[:8])
            + ("…" if len(names) > 8 else "")
        )
    if build_integrity.get("missing_situational_roles"):
        names = list(build_integrity["missing_situational_roles"])
        build_gap_parts.append(
            f"без situational {len(names)}: " + ", ".join(names[:8])
            + ("…" if len(names) > 8 else "")
        )
    if build_integrity.get("missing_opponent_adaptation_roles"):
        names = list(build_integrity["missing_opponent_adaptation_roles"])
        build_gap_parts.append(
            f"без opponent adaptations {len(names)}: " + ", ".join(names[:8])
            + ("…" if len(names) > 8 else "")
        )
    incomplete = list(build_integrity.get("incomplete_builds") or []) + list(
        build_integrity.get("incomplete_variants") or []
    )
    if incomplete:
        build_gap_parts.append(
            f"неполные блоки {len(incomplete)}: " + ", ".join(incomplete[:8])
            + ("…" if len(incomplete) > 8 else "")
        )
    if build_gap_parts:
        summary["errors"].append(
            "WildRiftCore build audit: " + "; ".join(build_gap_parts)
        )

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
    ci, ii, item_icon_failures = _cache_media(
        net, champs, items, ddragon_version, emit, lang,
        current_patch=current_patch, previous_patch=previous_patch, errors=summary["errors"],
        cancel_check=cancel_check, force_item_refresh=force_item_icon_refresh,
    )
    summary["champion_images"] = ci
    summary["item_images"] = ii
    if force_item_icon_refresh and item_icon_failures == 0:
        db.set_meta("item_icon_schema_version", ITEM_ICON_SCHEMA_VERSION)

    # Do not rely only on download exceptions. Verify the post-update state that
    # the UI will actually render: every final/referenced item must resolve to a
    # real image file. This makes "Скопировать лог" expose silent blanks too.
    missing_catalog, missing_icons = _audit_item_icon_integrity()
    summary["item_icon_missing_catalog"] = missing_catalog
    summary["item_icon_missing"] = missing_icons
    if missing_catalog or missing_icons:
        parts: list[str] = []
        if missing_catalog:
            parts.append(
                f"{len(missing_catalog)} отсутствуют в каталоге: "
                + ", ".join(missing_catalog[:20])
                + ("…" if len(missing_catalog) > 20 else "")
            )
        if missing_icons:
            parts.append(
                f"{len(missing_icons)} без валидной иконки: "
                + ", ".join(missing_icons[:20])
                + ("…" if len(missing_icons) > 20 else "")
            )
        summary["errors"].append("Item icon audit: " + "; ".join(parts))

    _check_cancel(cancel_check)
    stamp = format_update_timestamp(now)
    db.set_meta("last_update", stamp)
    db.set_meta("last_update_iso", now_iso)
    db.set_meta("source_note", "Champions/stats: ry2x; tiers/matchups/role-builds: WildRiftCore; counter-signals: WildRiftCounter; item catalog: Wild Rift Pocket; item media: WildRiftMeta + RiftGG + WildRiftCore exact-item fallback; RU shared item names: Riot Data Dragon + Wild Rift overrides")
    summary["errors"] = _collapse_dns_warnings(
        [str(value) for value in summary.get("errors", [])],
        net,
    )
    raw_progress(update_text("done", lang))
    return summary
