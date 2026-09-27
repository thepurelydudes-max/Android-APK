from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "WildRiftLocalAssistant/0.2 (+personal offline tool; respectful updater)",
    "Accept-Language": "en-US,en;q=0.9",
}

# WildRiftCore tier-list pages are SEO/server-rendered for normal browsers, but
# some CDN responses differ for non-browser user agents.  Keep the assistant
# identifier while presenting a browser-compatible HTML request so the public
# role tier tables are present in the returned document.
WR_CORE_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 "
        "WildRiftLocalAssistant/0.2"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CHAMPION_API_TEMPLATE = "https://ry2x.github.io/WildRift-Merged-Champion-Data/data_{locale}.json"
CHAMPION_API = CHAMPION_API_TEMPLATE.format(locale="en_US")
DDRAGON_VERSIONS = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_ITEMS_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/data/{locale}/item.json"
DDRAGON_CHAMPION_ICON = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champion_id}.png"
STATS_API = "https://ry2x.github.io/WildRift-Merged-Stats-Data/heroStats.json"
WR_COUNTER_HOME = "https://wildriftcounter.com/"
WR_COUNTER_CHAMPS = "https://wildriftcounter.com/champions/"
WR_CORE_CHAMPS = "https://wildriftcore.com/en/champions/"
WR_CORE_TIERLISTS = {
    "Барон": "https://wildriftcore.com/en/tierlist/baron-lane/",
    "Лес": "https://wildriftcore.com/en/tierlist/jungle/",
    "Мид": "https://wildriftcore.com/en/tierlist/mid-lane/",
    "ADC": "https://wildriftcore.com/en/tierlist/dragon-lane/",
    "Саппорт": "https://wildriftcore.com/en/tierlist/support/",
}
WR_POCKET_CHAMPS = "https://wrpocket.app/en/champions"
WR_POCKET_ITEMS = "https://wrpocket.app/en/items"
WR_META_CHAMPS = "https://www.wildriftmeta.com/champions/"
WR_POCKET_PATCH = "https://wrpocket.app/en/patch/7"
RIOT_PATCH_NOTES = "https://wildrift.leagueoflegends.com/en-us/news/tags/patch-notes/"


def is_dns_resolution_error(exc: BaseException | str) -> bool:
    """Recognise Android/urllib3 DNS failures without depending on exception internals."""
    text = str(exc or "").casefold()
    return any(token in text for token in (
        "nameresolutionerror",
        "failed to resolve",
        "name or service not known",
        "temporary failure in name resolution",
        "no address associated with hostname",
        "nodename nor servname provided",
        "getaddrinfo failed",
    ))


@dataclass
class Net:
    timeout: int = 25
    delay: float = 0.18

    def __post_init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        # Track DNS failures across independent hosts. If several unrelated
        # sources fail resolution in the same update, that is a device/network
        # DNS outage, not four separate parser failures.
        self._dns_failed_hosts: set[str] = set()
        self._dns_failure_events = 0
        self._wildriftcore_last_request = 0.0
        self._wildriftcore_gap = 1.35
        self._wildriftcounter_last_request = 0.0
        self._wildriftcounter_gap = 0.85

    def note_request_error(self, url: str, exc: BaseException) -> None:
        if not is_dns_resolution_error(exc):
            return
        host = (urlparse(str(url or "")).hostname or "").casefold()
        if host:
            self._dns_failed_hosts.add(host)
        self._dns_failure_events += 1

    @property
    def dns_outage(self) -> bool:
        # Two unrelated hosts failing DNS is already enough to distinguish a
        # local resolver/VPN problem from one site's temporary outage.
        return len(self._dns_failed_hosts) >= 2 or self._dns_failure_events >= 3

    @property
    def dns_failed_hosts(self) -> tuple[str, ...]:
        return tuple(sorted(self._dns_failed_hosts))

    def get(self, url: str, headers: dict | None = None, allow_not_modified: bool = False) -> requests.Response:
        # Android can briefly lose DNS while VPN/private-DNS routes are changing.
        # Retry a couple of times, but never turn one DNS outage into minutes of
        # repeated requests for every champion/image.
        waits = (0.8, 1.8)
        last_exc: requests.RequestException | None = None
        for attempt in range(len(waits) + 1):
            try:
                r = self.s.get(url, timeout=self.timeout, headers=headers or None)
                if not (allow_not_modified and r.status_code == 304):
                    r.raise_for_status()
                time.sleep(self.delay)
                return r
            except requests.RequestException as exc:
                last_exc = exc
                self.note_request_error(url, exc)
                if attempt >= len(waits):
                    raise
                time.sleep(waits[attempt])
        assert last_exc is not None
        raise last_exc


def _retry_after_seconds(response: requests.Response) -> float | None:
    value = clean(response.headers.get("Retry-After", ""))
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return max(0.0, min(seconds, 300.0))


def _wildriftcore_get(
    net: Net,
    url: str,
    progress: Callable[[str], None] | None = None,
    headers: dict | None = None,
) -> requests.Response:
    """Fetch one WildRiftCore page without hammering the site.

    WildRiftCore starts replying with HTTP 429 when ~140 counter pages are
    requested in a tight loop.  We therefore pace only this host, honour
    Retry-After when present and retry the *same* page instead of immediately
    sending another hundred doomed requests.  After the first 429 the spacing
    is increased for the remainder of the update.
    """
    fallback_waits = (15.0, 30.0, 60.0, 90.0, 120.0, 180.0)
    last_response: requests.Response | None = None

    for attempt in range(len(fallback_waits) + 1):
        gap = float(getattr(net, "_wildriftcore_gap", 1.35))
        last = float(getattr(net, "_wildriftcore_last_request", 0.0))
        elapsed = time.monotonic() - last
        if elapsed < gap:
            time.sleep(gap - elapsed)

        try:
            response = net.s.get(url, timeout=net.timeout, headers=headers or None)
        except requests.RequestException as exc:
            net._wildriftcore_last_request = time.monotonic()
            note_error = getattr(net, "note_request_error", None)
            if callable(note_error):
                note_error(url, exc)
            # DNS/transport failures use short retries. The long waits below are
            # reserved for an explicit server-side 429 Retry-After response.
            if attempt < 2:
                wait_seconds = (1.5, 3.0)[attempt]
                if progress:
                    progress(
                        f"WildRiftCore: ошибка соединения, повтор через "
                        f"{int(math.ceil(wait_seconds))} с…"
                    )
                time.sleep(wait_seconds)
                continue
            raise
        net._wildriftcore_last_request = time.monotonic()
        last_response = response

        if response.status_code == 429 and attempt < len(fallback_waits):
            header_wait = _retry_after_seconds(response)
            wait_seconds = max(header_wait or 0.0, fallback_waits[attempt])
            # Once throttled, keep subsequent requests deliberately slower.
            net._wildriftcore_gap = max(float(getattr(net, "_wildriftcore_gap", 1.35)), 3.0)
            if progress:
                progress(
                    f"WildRiftCore: лимит запросов (429). "
                    f"Пауза {int(math.ceil(wait_seconds))} с, затем повтор…"
                )
            time.sleep(wait_seconds)
            continue

        # A transient server-side failure is also worth a couple of retries,
        # but keep 4xx other than 429 visible immediately.
        if 500 <= response.status_code < 600 and attempt < min(3, len(fallback_waits)):
            wait_seconds = 4.0 * (2 ** attempt)
            if progress:
                progress(
                    f"WildRiftCore: временная ошибка {response.status_code}. "
                    f"Повтор через {int(wait_seconds)} с…"
                )
            time.sleep(wait_seconds)
            continue

        response.raise_for_status()
        return response

    assert last_response is not None
    last_response.raise_for_status()
    return last_response


def _wildriftcounter_get(
    net: Net,
    url: str,
    progress: Callable[[str], None] | None = None,
) -> requests.Response:
    """Fetch WildRiftCounter pages slowly and retry transient disconnects.

    The host sometimes closes keep-alive connections while many champion pages
    are requested in sequence. One RemoteDisconnected must not abort the whole
    counter-item refresh immediately.
    """
    waits = (1.5, 3.0, 6.0, 10.0)
    last_exc: Exception | None = None
    for attempt in range(len(waits) + 1):
        last = float(getattr(net, "_wildriftcounter_last_request", 0.0))
        gap = float(getattr(net, "_wildriftcounter_gap", 0.85))
        elapsed = time.monotonic() - last
        if elapsed < gap:
            time.sleep(gap - elapsed)
        try:
            response = net.s.get(url, timeout=net.timeout)
            net._wildriftcounter_last_request = time.monotonic()
            if response.status_code == 429 and attempt < len(waits):
                wait = max(_retry_after_seconds(response) or 0.0, waits[attempt])
                net._wildriftcounter_gap = max(gap, 1.5)
                if progress:
                    progress(f"WildRiftCounter: 429, повтор через {int(math.ceil(wait))} с…")
                time.sleep(wait)
                continue
            if 500 <= response.status_code < 600 and attempt < len(waits):
                wait = waits[attempt]
                if progress:
                    progress(
                        f"WildRiftCounter: временная ошибка {response.status_code}, "
                        f"повтор через {int(math.ceil(wait))} с…"
                    )
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            net._wildriftcounter_last_request = time.monotonic()
            note_error = getattr(net, "note_request_error", None)
            if callable(note_error):
                note_error(url, exc)
            # A DNS resolver failure will not be fixed by hammering the same
            # hostname five times. Two short retries are enough.
            if attempt >= len(waits) or (is_dns_resolution_error(exc) and attempt >= 2):
                raise
            wait = waits[attempt]
            if progress:
                progress(
                    f"WildRiftCounter: соединение прервано, повтор через "
                    f"{int(math.ceil(wait))} с…"
                )
            time.sleep(wait)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("WildRiftCounter request failed")


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def clean_item_name(s: str) -> str:
    """Normalize punctuation and repair the common UTF-8-as-cp1252 apostrophe corruption."""
    value = clean(s)
    value = value.replace("â", "’").replace("â€™", "’").replace("`", "'")
    return value


def slugish(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.casefold().replace("’", "'")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def fetch_champions_locale(net: Net, locale: str = "en_US") -> list[dict]:
    data = net.get(CHAMPION_API_TEMPLATE.format(locale=locale)).json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("champions") or list(data.values())
    out = []
    for c in data:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        if c.get("is_wr") is False:
            continue
        out.append({
            "id": str(c.get("id")),
            "name": str(c.get("name") or c.get("id")),
            "roles": list(c.get("roles") or []),
            "lanes": list(c.get("lanes") or []),
            "damage_type": str(c.get("type") or ""),
        })
    return out


def fetch_champions(net: Net) -> list[dict]:
    return fetch_champions_locale(net, "en_US")


def fetch_wildriftmeta_champion_roster(net: Net) -> list[dict]:
    """Supplement the master champion feed with current WR-only roster entries.

    The ry2x merged feed can lag a newly released Wild Rift champion. WildRiftMeta
    currently exposes the complete live roster as canonical /champions/<slug>/
    links. We use it only to add missing identities; existing champion metadata
    from the structured feed always wins.
    """
    soup = BeautifulSoup(net.get(WR_META_CHAMPS).text, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        m = re.fullmatch(r"/champions/([^/?#]+)/?", href)
        if not m:
            continue
        slug = clean(m.group(1))
        key = slugish(slug)
        if not key or key in seen:
            continue
        label = clean(a.get_text(" ", strip=True))
        # Only trust link text when it describes exactly the same identity as
        # the URL slug. Card text may contain tier/win-rate decorations.
        fallback_name = slug.replace("-", " ").title()
        name = label if label and slugish(label) == key else fallback_name
        name = clean(name)
        if not name:
            continue
        seen.add(key)
        out.append({
            "id": name,
            "name": name,
            "name_ru": "",
            "roles": [],
            "lanes": [],
            "damage_type": "",
            "icon_url": f"https://www.wildriftmeta.com/assets/champion/icon/champion-{slug}-icon.png",
            "profile_slug": slug,
        })
    return out


def fetch_ddragon_version(net: Net) -> str:
    data = net.get(DDRAGON_VERSIONS).json()
    if not isinstance(data, list) or not data:
        raise RuntimeError("Data Dragon version list is empty")
    return str(data[0])


def fetch_ddragon_item_ru_map(net: Net, version: str) -> dict[str, str]:
    en = net.get(DDRAGON_ITEMS_TEMPLATE.format(version=version, locale="en_US")).json().get("data", {})
    ru = net.get(DDRAGON_ITEMS_TEMPLATE.format(version=version, locale="ru_RU")).json().get("data", {})
    out: dict[str, str] = {}
    for item_id, row in en.items():
        en_name = clean(str((row or {}).get("name") or ""))
        ru_name = clean(str((ru.get(item_id) or {}).get("name") or ""))
        if en_name and ru_name:
            out[en_name.casefold().replace("’", "'")] = ru_name
    return out


def _pct(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) * 100 if 0 <= float(v) <= 1 else float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group()) if m else None


def fetch_stats(net: Net) -> tuple[str, list[dict]]:
    root = net.get(STATS_API).json()
    date = str(root.get("date") or "") if isinstance(root, dict) else ""
    data = root.get("data", root) if isinstance(root, dict) else {}
    rows = []
    for rank, lane_map in (data or {}).items():
        if not isinstance(lane_map, dict):
            continue
        for lane, arr in lane_map.items():
            if not isinstance(arr, list):
                continue
            for s in arr:
                champ = s.get("id") or s.get("champion_id")
                if not champ:
                    continue
                rows.append({
                    "champion_id": str(champ),
                    "lane": str(lane),
                    "rank": str(rank),
                    "win_rate": _pct(s.get("win_rate_percent") or s.get("win_rate_float") or s.get("win_rate")),
                    "pick_rate": _pct(s.get("appear_rate_percent") or s.get("appear_rate_float") or s.get("appear_rate")),
                    "ban_rate": _pct(s.get("forbid_rate_percent") or s.get("forbid_rate_float") or s.get("forbid_rate")),
                })
    return date, rows


def _wildriftcore_role(value: str) -> str:
    """Map WildRiftCore lane labels to the role labels used by the UI/engine."""
    role = clean(value).casefold()
    if role in {"top", "top lane", "baron", "baron lane"}:
        return "Барон"
    if role in {"jungle", "jg"}:
        return "Лес"
    if role in {"mid", "mid lane", "middle"}:
        return "Мид"
    if role in {"adc", "dragon", "dragon lane", "bot", "bottom", "bottom lane"}:
        return "ADC"
    if role in {"support", "sup"}:
        return "Саппорт"
    return ""


def _wildriftcore_edge(value: str) -> float | None:
    """Parse the canonical WildRiftCore Edge value (−3..+3, including ±0)."""
    text = clean(value).replace("−", "-").replace("–", "-").replace("—", "-")
    text = text.replace("±", "")
    m = re.search(r"[+-]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        score = float(m.group())
    except ValueError:
        return None
    # Guard against accidentally parsing a percentage/other table column if the site changes.
    if score < -3.0 or score > 3.0:
        return None
    return score


def _wildriftcore_profile_links(
    net: Net,
    resolve: Callable[[str], str | None],
    progress: Callable[[str], None] | None = None,
) -> list[tuple[str, str]]:
    """Discover champion profile URLs from the current WildRiftCore champion index.

    Discovering links from the index is intentionally preferred over generating slugs:
    names such as Kha'Zix, K'Santé and Nunu & Willump have site-specific URL forms.
    """
    html = _wildriftcore_get(net, WR_CORE_CHAMPS, progress).text
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        m = re.search(r"/en/champions/([^/?#]+)/?$", href)
        if not m:
            continue
        slug = m.group(1)
        cid = resolve(slug)
        if not cid:
            # Fallback for a future harmless URL/name mismatch. Card text may contain a tier
            # suffix, so try only the leading text up to the first obvious tier token.
            raw = clean(a.get_text(" ", strip=True))
            cid = resolve(raw)
        if cid:
            found[cid] = urljoin(WR_CORE_CHAMPS, href)
    return sorted(found.items(), key=lambda row: row[0].casefold())


def _parse_wildriftcore_counter_page(
    html: str, owner_id: str, resolve: Callable[[str], str | None]
) -> list[tuple[str, str, str, float]]:
    """Parse one champion's role-specific matchup boards from WildRiftCore.

    WildRiftCore documents Edge from the point of view of the champion named in each
    matchup row. On the owner's counter page that means a row ``Vayne +2`` under
    Darius says Vayne has +2 into Darius, so Darius' direct score into Vayne is -2.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[tuple[str, str, str, float]] = []

    for heading in soup.find_all(["h2", "h3"]):
        heading_text = clean(heading.get_text(" ", strip=True))
        m = re.search(r"How to counter .+? in (.+?):\s*the essentials", heading_text, flags=re.I)
        if not m:
            continue
        role = _wildriftcore_role(m.group(1))
        if not role:
            continue

        table = heading.find_next("table")
        if table is None:
            continue
        first = table.find("tr")
        if first is None:
            continue
        headers = [clean(x.get_text(" ", strip=True)).casefold() for x in first.find_all(["th", "td"])]
        try:
            matchup_idx = headers.index("matchup")
            edge_idx = headers.index("edge")
        except ValueError:
            continue

        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= max(matchup_idx, edge_idx):
                continue
            opponent_name = clean(cells[matchup_idx].get_text(" ", strip=True))
            opponent_id = resolve(opponent_name)
            edge_for_opponent = _wildriftcore_edge(cells[edge_idx].get_text(" ", strip=True))
            if not opponent_id or opponent_id == owner_id or edge_for_opponent is None:
                continue
            rows.append((owner_id, opponent_id, role, -edge_for_opponent))
    return rows


def parse_wildriftcore_matchups(
    net: Net,
    resolve: Callable[[str], str | None],
    progress: Callable[[str], None] | None = None,
) -> list[tuple[str, str, str, float]]:
    """Fetch the full role-aware WildRiftCore counter matrix with resumable cache.

    Every successfully parsed champion page is written to the local SQLite cache
    immediately. If an update is interrupted or WildRiftCore answers with 429, the
    next update on the same game patch skips those champions instead of downloading
    them again. A new patch automatically gets its own fresh cache.
    """
    profiles = _wildriftcore_profile_links(net, resolve, progress)
    if not profiles:
        raise RuntimeError("Не найдены страницы чемпионов на WildRiftCore")

    # Import locally to keep sources.py usable by lightweight parser tests.
    try:
        import db as _db
        cache_patch = _db.get_meta("patch_version", "") or "unknown"
        cached_pages = _db.get_matchup_page_cache("wildriftcore.com", cache_patch)
    except Exception:
        _db = None
        cache_patch = "unknown"
        cached_pages = {}

    direct: dict[tuple[str, str, str], float] = {}
    errors: list[str] = []
    successful_pages = 0
    reused_pages = 0
    downloaded_pages = 0
    total = len(profiles)

    for idx, (owner_id, profile_url) in enumerate(profiles, 1):
        cached_rows = cached_pages.get(owner_id) or []
        if cached_rows:
            successful_pages += 1
            reused_pages += 1
            for champion_id, enemy_id, role, score in cached_rows:
                direct[(champion_id, enemy_id, role)] = score
            if progress:
                progress(
                    f"WildRiftCore matchups: {idx}/{total} — {owner_id} "
                    f"(из кэша {reused_pages}, скачано {downloaded_pages})"
                )
            continue

        if progress:
            progress(
                f"WildRiftCore matchups: {idx}/{total} — {owner_id} "
                f"(из кэша {reused_pages}, скачано {downloaded_pages})"
            )
        counters_url = profile_url.rstrip("/") + "/counters/"
        try:
            html = _wildriftcore_get(net, counters_url, progress).text
            page_rows = _parse_wildriftcore_counter_page(html, owner_id, resolve)
            if page_rows:
                successful_pages += 1
                downloaded_pages += 1
                for champion_id, enemy_id, role, score in page_rows:
                    direct[(champion_id, enemy_id, role)] = score
                # Save immediately, not at the end of the 140-page run.
                if _db is not None:
                    try:
                        _db.upsert_matchup_page_cache(
                            "wildriftcore.com", cache_patch, owner_id, page_rows, counters_url
                        )
                        cached_pages[owner_id] = list(page_rows)
                    except Exception:
                        pass
            else:
                errors.append(f"{owner_id}: на странице не найдены matchup-строки")
        except requests.HTTPError as exc:
            response = getattr(exc, "response", None)
            if response is not None and response.status_code == 429:
                # Do not continue firing requests after the host has rate-limited us.
                # All pages parsed before this point are already safely cached.
                raise RuntimeError(
                    f"WildRiftCore временно ограничил запросы (429). "
                    f"Сохранено {successful_pages}/{total} страниц; "
                    f"при следующем обновлении они будут взяты из кэша."
                ) from exc
            errors.append(f"{owner_id}: {exc}")
        except Exception as exc:
            errors.append(f"{owner_id}: {exc}")

    min_success = min(total, max(10, int(total * 0.70)))
    if not direct or successful_pages < min_success:
        detail = f" ({'; '.join(errors[:3])})" if errors else ""
        raise RuntimeError(
            f"WildRiftCore вернул неполную матрицу: {successful_pages}/{total} страниц. "
            f"Уже скачанные страницы сохранены и повторно загружаться не будут" + detail
        )

    # Fill only genuinely missing reverse directions. Never overwrite an explicit
    # verdict from the reverse champion's own page, even if the site is asymmetric.
    completed = dict(direct)
    for (champion_id, enemy_id, role), score in list(direct.items()):
        completed.setdefault((enemy_id, champion_id, role), -score)

    if _db is not None:
        try:
            _db.prune_matchup_page_cache("wildriftcore.com", cache_patch)
        except Exception:
            pass

    if progress:
        progress(
            f"WildRiftCore: матрица готова — {successful_pages}/{total} страниц "
            f"(из кэша {reused_pages}, скачано {downloaded_pages})."
        )

    return [
        (champion_id, enemy_id, role, score)
        for (champion_id, enemy_id, role), score in sorted(completed.items())
    ]


def _wildriftcore_champion_slug(href: str) -> str:
    """Return a champion slug from a WildRiftCore champion URL.

    The site has used plain links, trailing slashes and links with query/hash
    parameters over time.  Tier parsing must accept all of them.
    """
    value = str(href or "").strip()
    m = re.search(r"/(?:[a-z]{2}/)?champions/([^/?#]+)(?:/)?(?:[?#].*)?$", value, re.I)
    return clean(m.group(1)) if m else ""


def _wildriftcore_ranked_section_ids(
    html: str,
    resolve: Callable[[str], str | None],
) -> tuple[set[str], list[str]]:
    """Return every champion identity advertised inside the ranked tier section.

    This is an integrity guard, not a second tier parser. A role page must not be
    allowed to replace the database with a partial parse merely because more than
    ten rows happened to survive a markup change. We compare the tier rows we
    parsed with the champion profile links actually present in the same ranked
    section. Any resolvable champion link without a tier row means the refresh is
    incomplete and the updater must preserve the last known-good role table.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    start_node = None
    for node in soup.find_all(["h1", "h2", "h3", "h4", "div", "p"]):
        text = clean(node.get_text(" ", strip=True))
        folded = text.casefold()
        if "champion ranked" in folded and "s+" in folded and "to c" in folded:
            start_node = node
            break

    if start_node is None:
        return set(), []

    resolved: set[str] = set()
    unresolved: list[str] = []
    for node in start_node.find_all_next():
        if not getattr(node, "name", None):
            continue
        text = clean(node.get_text(" ", strip=True))
        folded = text.casefold()
        if node.name in {"h1", "h2", "h3", "h4"} and (
            "how do we calculate this tier list" in folded
            or "now, prepare your next game" in folded
        ):
            break
        if node.name != "a":
            continue
        slug = _wildriftcore_champion_slug(str(node.get("href") or ""))
        if not slug:
            continue
        cid = resolve(slug) or resolve(clean(node.get_text(" ", strip=True)))
        if cid:
            resolved.add(cid)
        elif slug not in unresolved:
            unresolved.append(slug)
    return resolved, unresolved


def _parse_wildriftcore_tier_page(
    html: str,
    role: str,
    resolve: Callable[[str], str | None],
) -> list[tuple[str, str, str]]:
    """Parse one role-specific WildRiftCore tier-list page.

    The current public pages contain a section headed approximately
    ``Every <role> champion ranked, S+ to C``.  Inside it the DOM groups
    champions under S+/S/A/B/C headings.  Older versions also repeated the
    tier inside every champion card.  Parse the section structure first and
    keep two independent fallbacks, so harmless markup changes do not make the
    whole local tier table disappear.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, str] = {}
    valid_tiers = {"S+", "S", "A", "B", "C", "D"}
    tier_token = re.compile(r"(?<![A-Z0-9])(S\+|S|A|B|C|D)(?![A-Z0-9])", re.I)

    # ------------------------------------------------------------------
    # 1) Current layout: section heading -> tier heading -> champion links.
    # ------------------------------------------------------------------
    start_node = None
    for node in soup.find_all(["h1", "h2", "h3", "h4", "div", "p"]):
        text = clean(node.get_text(" ", strip=True))
        folded = text.casefold()
        if "champion ranked" in folded and "s+" in folded and "to c" in folded:
            start_node = node
            break

    if start_node is not None:
        current_tier = ""
        # find_all_next() preserves document order.  Stop before explanatory
        # content so names in examples/FAQ cannot be misclassified.
        for node in start_node.find_all_next():
            if not getattr(node, "name", None):
                continue
            text = clean(node.get_text(" ", strip=True))
            folded = text.casefold()
            if node.name in {"h1", "h2", "h3", "h4"} and (
                "how do we calculate this tier list" in folded
                or "now, prepare your next game" in folded
            ):
                break

            # Prefer compact heading-like elements.  Exact text avoids treating
            # the S in a champion/card sentence as a new section.
            if text.upper() in valid_tiers and node.name in {"h2", "h3", "h4", "h5", "div", "span", "p", "strong"}:
                current_tier = text.upper()
                continue

            if node.name != "a" or not current_tier:
                continue
            slug = _wildriftcore_champion_slug(str(node.get("href") or ""))
            if not slug:
                continue
            # Link text may be just "Rammus" or the whole
            # "S+ Rammus 55.6% WR ..." card.  Slug is the safest resolver.
            cid = resolve(slug) or resolve(clean(node.get_text(" ", strip=True)))
            if cid:
                out[cid] = current_tier

    # ------------------------------------------------------------------
    # 2) Card/link fallback: tier token is inside the champion link/card.
    # ------------------------------------------------------------------
    for a in soup.find_all("a", href=True):
        slug = _wildriftcore_champion_slug(str(a.get("href") or ""))
        if not slug:
            continue
        cid = resolve(slug) or resolve(clean(a.get_text(" ", strip=True)))
        if not cid or cid in out:
            continue

        candidates = [a]
        parent = a
        for _ in range(7):
            parent = getattr(parent, "parent", None)
            if parent is None:
                break
            # Do not climb into a container that contains many champion links;
            # otherwise a tier from another card can leak into this champion.
            champ_links = [
                x for x in parent.find_all("a", href=True)
                if _wildriftcore_champion_slug(str(x.get("href") or ""))
            ]
            if len(champ_links) <= 1:
                candidates.append(parent)
            else:
                break

        for node in candidates:
            m = tier_token.search(clean(node.get_text(" ", strip=True)))
            if m:
                out[cid] = m.group(1).upper()
                break

    # ------------------------------------------------------------------
    # 3) Flattened-text fallback.  On some SSR variants BeautifulSoup splits
    #    a card into several strings: "S+" / "Rammus" / "55.6% WR".
    #    Track the current tier heading and resolve champion-name strings.
    # ------------------------------------------------------------------
    strings = [clean(x) for x in soup.stripped_strings if clean(x)]
    in_ranked_section = False
    current_tier = ""
    for text in strings:
        folded = text.casefold()
        if "champion ranked" in folded and "s+" in folded and "to c" in folded:
            in_ranked_section = True
            current_tier = ""
            continue
        if not in_ranked_section:
            continue
        if "how do we calculate this tier list" in folded or "now, prepare your next game" in folded:
            break
        upper = text.upper()
        if upper in valid_tiers:
            current_tier = upper
            continue
        if not current_tier:
            continue

        # Exact visible champion names are preferred.  If a line includes the
        # tier + name + WR/PR/BR, trim those decorations before resolving.
        candidate = re.sub(r"^(?:NO\.\s*\d+\s+|\d+\s+)?(?:S\+|S|A|B|C|D)\s+", "", text, flags=re.I)
        candidate = re.sub(r"\s+\d+(?:\.\d+)?%\s+WR.*$", "", candidate, flags=re.I)
        candidate = clean(candidate)
        cid = resolve(candidate)
        if cid:
            out.setdefault(cid, current_tier)

    return [(champion_id, role, tier) for champion_id, tier in sorted(out.items())]


def parse_wildriftcore_tiers(
    net: Net,
    resolve: Callable[[str], str | None],
    progress: Callable[[str], None] | None = None,
) -> list[tuple[str, str, str]]:
    """Fetch current role-specific S+/S/A/B/C placements from WildRiftCore.

    A failure on one role no longer discards four successfully parsed roles.
    The updater writes only the roles returned here and keeps older rows for a
    temporarily unavailable role.
    """
    rows: list[tuple[str, str, str]] = []
    errors: list[str] = []
    succeeded_roles: list[str] = []

    for idx, (role, url) in enumerate(WR_CORE_TIERLISTS.items(), 1):
        if progress:
            progress(f"WildRiftCore tiers: {idx}/{len(WR_CORE_TIERLISTS)} — {role}")
        try:
            response = _wildriftcore_get(net, url, progress, headers=WR_CORE_HTML_HEADERS)
            html = response.text
            parsed = _parse_wildriftcore_tier_page(html, role, resolve)
            expected_ids, unresolved_slugs = _wildriftcore_ranked_section_ids(html, resolve)
            parsed_ids = {champion_id for champion_id, _role, _tier in parsed}

            # A tiny result means the site returned a shell/challenge or markup
            # changed, so do not overwrite that role with partial data.
            if len(parsed) < 10:
                title = ""
                try:
                    title = clean(BeautifulSoup(html, "html.parser").title.get_text(" ", strip=True))
                except Exception:
                    pass
                detail = f"; title={title!r}" if title else ""
                raise RuntimeError(f"найдено только {len(parsed)} tier-строк{detail}")

            # Strong integrity guard: every champion link present in the ranked
            # section that resolves to our champion database must also have a
            # parsed tier. Previously ">=10 rows" could declare a half-parsed
            # page successful and then erase the missing champions' old tiers.
            missing_ids = sorted(expected_ids - parsed_ids)
            if unresolved_slugs or missing_ids:
                details = []
                if missing_ids:
                    details.append("без tier: " + ", ".join(missing_ids[:12]))
                if unresolved_slugs:
                    details.append("не распознаны URL: " + ", ".join(unresolved_slugs[:12]))
                raise RuntimeError(
                    f"неполный tier parse {len(parsed_ids)}/{len(expected_ids) or '?'}; "
                    + "; ".join(details)
                )
            rows.extend(parsed)
            succeeded_roles.append(role)
            if progress:
                progress(f"WildRiftCore tiers: {role} — {len(parsed)} чемпионов")
        except Exception as exc:
            errors.append(f"{role}: {exc}")
            if progress:
                progress(f"WildRiftCore tiers: {role} — ошибка: {exc}")

    if not rows:
        detail = "; ".join(errors[:5])
        raise RuntimeError("WildRiftCore tier list не удалось прочитать" + (f" ({detail})" if detail else ""))

    # Duplicate rows should never occur for the same role+champion, but make
    # the output deterministic if the site repeats a card in desktop/mobile DOM.
    dedup: dict[tuple[str, str], tuple[str, str, str]] = {}
    for champion_id, role, tier in rows:
        dedup[(champion_id, role)] = (champion_id, role, tier)
    rows = list(dedup.values())

    if errors and progress:
        progress(
            "WildRiftCore tiers: частичное обновление; сохранены роли "
            + ", ".join(succeeded_roles)
            + ". Не обновились: "
            + "; ".join(errors)
        )
    return rows


def _known_item_lookup(known_items: Iterable[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in known_items or []:
        name = canonical_item_name(str(raw or ""))
        key = slugish(name)
        if name and key:
            out[key] = name
    return out


def _canonical_known_item(value: str, lookup: dict[str, str]) -> str:
    cleaned = canonical_item_name(clean_item_name(value))
    return lookup.get(slugish(cleaned), cleaned)


def _looks_like_boot_name(name: str) -> bool:
    text = clean_item_name(name).casefold()
    return any(token in text for token in (
        "boots", "treads", "greaves", "shoes", "steelcaps", "advance",
        "crushers", "lucidity",
    ))


# Kept as a compatibility symbol for older code/tests. Patch 7.3 sources such
# as WildRiftMeta/WildRiftCore publish Mercury's Treads, Plated Steelcaps,
# Boots of Mana, etc. as the actual current boots. Do not rewrite those names
# into historical upgrade names.
BOOT_UPGRADE_MAP: dict[str, str] = {}


def _upgrade_boot_name(value: str, lookup: dict[str, str]) -> str:
    """Canonicalise a source-provided boot without changing its identity."""
    return _canonical_known_item(clean_item_name(value), lookup)


def parse_wildriftcore_build_page(
    html: str,
    champion_id: str,
    known_items: Iterable[str],
) -> dict:
    """Parse role-specific standard builds and approved adaptations from one WRC page.

    WildRiftCore publishes a plain-language summary for every supported role:
    "Best X build (Role): item › item ..., boots Y, keystone Z."  That sentence is
    the authoritative core.  Situational items are accepted only from the same
    role section, so the assistant never invents tank/ADC items for a mage merely
    because a global item happens to counter the enemy.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    lines = [clean(x) for x in soup.stripped_strings if clean(x)]
    lookup = _known_item_lookup(known_items)
    builds: list[dict] = []
    situational: list[dict] = []
    boots: list[dict] = []

    summary_re = re.compile(
        r"Best\s+.+?\s+build\s*\(([^)]+)\):\s*(.+?),\s*boots\s+(.+?),\s*keystone\b",
        flags=re.I,
    )

    role_starts: list[tuple[int, str]] = []
    seen_roles: set[str] = set()

    for idx, line in enumerate(lines):
        m = summary_re.search(line)
        if not m:
            continue
        role = _wildriftcore_role(m.group(1))
        if not role or role in seen_roles:
            continue
        raw_items = [
            clean_item_name(x)
            for x in re.split(r"\s*[›>→]\s*", m.group(2))
            if clean_item_name(x)
        ]
        items = []
        for raw in raw_items:
            item = _canonical_known_item(raw, lookup)
            if item and item not in items:
                items.append(item)
        boot = _upgrade_boot_name(m.group(3), lookup)
        if len(items) < 3:
            continue
        seen_roles.add(role)
        role_starts.append((idx, role))
        builds.append({"role": role, "items": items[:5], "boot": boot})
        if boot:
            boots.append({
                "role": role, "item": boot, "trigger": "standard",
                "priority": 0,
            })

    # Some HTML variants split one role summary across adjacent text nodes.
    # Always scan the flattened page for roles that were not recovered above.
    flat = clean(" ".join(lines))
    for m in summary_re.finditer(flat):
        role = _wildriftcore_role(m.group(1))
        if not role or role in seen_roles:
            continue
        raw_items = [clean_item_name(x) for x in re.split(r"\s*[›>→]\s*", m.group(2)) if clean_item_name(x)]
        items = []
        for raw in raw_items:
            item = _canonical_known_item(raw, lookup)
            if item and item not in items:
                items.append(item)
        boot = _upgrade_boot_name(m.group(3), lookup)
        if len(items) >= 3:
            seen_roles.add(role)
            builds.append({"role": role, "items": items[:5], "boot": boot})
            if boot:
                boots.append({"role": role, "item": boot, "trigger": "standard", "priority": 0})

    # If a summary was found only in flattened text, recover section starts from
    # the role headings so the role-local situational list still parses correctly.
    if builds:
        build_roles = {row["role"] for row in builds}
        existing_starts = {role for _idx, role in role_starts}
        for idx, line in enumerate(lines):
            folded = line.casefold()
            if "recommended build" not in folded:
                continue
            for role_label in ("baron lane", "top lane", "jungle", "mid lane", "adc", "dragon lane", "support"):
                if role_label in folded:
                    role = _wildriftcore_role(role_label)
                    if role in build_roles and role not in existing_starts:
                        role_starts.append((idx, role))
                        existing_starts.add(role)
                    break

    # Parse the role-local "Situational adaptations" section.  We deliberately
    # match only names already known in the finished item catalog.
    known_by_length = sorted(lookup.values(), key=len, reverse=True)
    start_by_role = {role: idx for idx, role in role_starts}
    ordered_starts = sorted(role_starts)

    for pos, (start_idx, role) in enumerate(ordered_starts):
        end_idx = ordered_starts[pos + 1][0] if pos + 1 < len(ordered_starts) else len(lines)
        for j in range(start_idx + 1, end_idx):
            if lines[j].casefold().startswith("core build") and "historical" in lines[j].casefold():
                end_idx = j
                break
        segment = lines[start_idx:end_idx]
        try:
            sec_start = next(i for i, value in enumerate(segment) if value.casefold() == "situational adaptations")
        except StopIteration:
            continue
        sec_end = len(segment)
        for i in range(sec_start + 1, len(segment)):
            folded = segment[i].casefold()
            if folded.startswith("adaptations by opponent") or folded.startswith("power vs."):
                sec_end = i
                break

        current_item = ""
        priority = 1
        body = [clean(x) for x in segment[sec_start + 1:sec_end] if clean(x)]
        for line_idx, line in enumerate(body):
            exact = lookup.get(slugish(line))
            if exact:
                current_item = exact
                continue

            inline_item = ""
            inline_reason = ""
            for candidate in known_by_length:
                if line.casefold().startswith(candidate.casefold()):
                    rest = clean(line[len(candidate):])
                    if rest.casefold().startswith(("against ", "vs ")):
                        inline_item = candidate
                        inline_reason = rest
                        break
            if inline_item:
                situational.append({
                    "role": role, "item": inline_item, "trigger": inline_reason,
                    "priority": priority,
                })
                priority += 1
                current_item = ""
                continue

            if current_item and line.casefold().startswith(("against ", "vs ")):
                situational.append({
                    "role": role, "item": current_item, "trigger": line,
                    "priority": priority,
                })
                priority += 1
                current_item = ""
                continue

            next_line = body[line_idx + 1] if line_idx + 1 < len(body) else ""
            if (
                next_line.casefold().startswith(("against ", "vs "))
                and len(line) <= 80
                and not line.casefold().startswith(("when to pick", "example", "adaptations"))
            ):
                current_item = canonical_item_name(line)

        # Exact opponent -> item adaptations published for this role are a
        # stronger source signal than broad inferred enemy tags.
        opponent_start = -1
        for i, value in enumerate(segment):
            if value.casefold().startswith("adaptations by opponent"):
                opponent_start = i + 1
                break
        if opponent_start >= 0:
            role_items = list(known_by_length)
            role_items.extend(
                row["item"] for row in situational
                if row.get("role") == role and row.get("item") not in role_items
            )
            role_items = sorted(set(role_items), key=len, reverse=True)
            opp_priority = 100
            opp_lines = [clean(x) for x in segment[opponent_start:] if clean(x)]
            parsed_pairs: list[tuple[str, str]] = []
            for line_idx, line in enumerate(opp_lines):
                if "›" in line and line != "›":
                    enemy_name, right = [clean(x) for x in line.split("›", 1)]
                    if enemy_name and right:
                        parsed_pairs.append((enemy_name, right))
                    continue
                if line == "›" and line_idx > 0 and line_idx + 1 < len(opp_lines):
                    parsed_pairs.append((opp_lines[line_idx - 1], opp_lines[line_idx + 1]))

            for enemy_name, right in parsed_pairs:
                chosen = ""
                reason = ""
                for candidate in role_items:
                    if right.casefold().startswith(candidate.casefold()):
                        chosen = candidate
                        reason = clean(right[len(candidate):])
                        break
                if not chosen:
                    exact = lookup.get(slugish(right))
                    if exact:
                        chosen = exact
                if not chosen:
                    continue
                situational.append({
                    "role": role, "item": chosen,
                    "trigger": f"Opponent: {enemy_name}" + (f"; {reason}" if reason else ""),
                    "priority": opp_priority,
                })
                opp_priority += 1

    # Historical "Boots & enchant" is used only to discover champion-approved
    # alternatives.  It never creates a boot that is not already on this page.
    historical_boots: list[tuple[str, str]] = []
    boot_section = -1
    for idx, line in enumerate(lines):
        if line.casefold() in {"boots & enchant", "boots and enchant"}:
            boot_section = idx
            break
    if boot_section >= 0:
        end = min(len(lines), boot_section + 40)
        for idx in range(boot_section + 1, end):
            if lines[idx].casefold().startswith("skill order"):
                end = idx
                break
        pending_reason = ""
        for line in lines[boot_section + 1:end]:
            folded = line.casefold()
            if folded.startswith(("alternative", "vs ", "against ")):
                pending_reason = line
                continue
            item = _upgrade_boot_name(line, lookup)
            if item and slugish(item) in {slugish(x) for x in lookup.values()} and _looks_like_boot_name(item):
                historical_boots.append((item, pending_reason))
                pending_reason = ""

    for build in builds:
        role = build["role"]
        baseline = build.get("boot") or ""
        priority = 10
        for item, reason in historical_boots:
            if not item or item == baseline:
                continue
            if any(row["role"] == role and row["item"] == item for row in boots):
                continue
            boots.append({
                "role": role, "item": item,
                "trigger": reason or "alternative",
                "priority": priority,
            })
            priority += 1

    # Deduplicate while preserving source order.
    dedup_sit: list[dict] = []
    sit_index: dict[tuple[str, str], dict] = {}
    for row in situational:
        key = (row["role"], row["item"])
        existing = sit_index.get(key)
        if existing is None:
            copied = dict(row)
            sit_index[key] = copied
            dedup_sit.append(copied)
            continue
        trigger = clean(str(row.get("trigger") or ""))
        old_trigger = clean(str(existing.get("trigger") or ""))
        if trigger and trigger not in old_trigger:
            existing["trigger"] = old_trigger + (" || " if old_trigger else "") + trigger
        existing["priority"] = min(
            int(existing.get("priority") or 999),
            int(row.get("priority") or 999),
        )

    dedup_boots: list[dict] = []
    seen_boots: set[tuple[str, str]] = set()
    for row in boots:
        key = (row["role"], row["item"])
        if key in seen_boots:
            continue
        seen_boots.add(key)
        dedup_boots.append(row)

    return {
        "champion_id": champion_id,
        "builds": builds,
        "situational": dedup_sit,
        "boots": dedup_boots,
    }


def fetch_wildriftcore_role_builds(
    net: Net,
    resolve: Callable[[str], str | None],
    known_items: Iterable[str],
    progress: Callable[[str], None] | None = None,
) -> tuple[
    list[tuple[str, str, list[str], str, str, str]],
    list[tuple[str, str, str, str, int]],
    list[tuple[str, str, str, str, int]],
    list[str],
]:
    """Fetch/cache champion+role builds from WildRiftCore.

    The cache is per patch and per champion.  Partial failures preserve previously
    stored role builds instead of erasing them.
    """
    profiles = _wildriftcore_profile_links(net, resolve, progress)
    if not profiles:
        raise RuntimeError("Не найдены страницы сборок WildRiftCore")

    try:
        import db as _db
        cache_patch = _db.get_meta("patch_version", "") or "unknown"
        cached_pages = _db.get_build_page_cache("wildriftcore.com", cache_patch)
    except Exception:
        _db = None
        cache_patch = "unknown"
        cached_pages = {}

    builds_out: list[tuple[str, str, list[str], str, str, str]] = []
    sit_out: list[tuple[str, str, str, str, int]] = []
    boots_out: list[tuple[str, str, str, str, int]] = []
    errors: list[str] = []
    success = 0
    reused = 0
    downloaded = 0
    total = len(profiles)

    for idx, (champion_id, profile_url) in enumerate(profiles, 1):
        build_url = profile_url.rstrip("/") + "/builds/"
        payload = cached_pages.get(champion_id)
        if payload:
            reused += 1
        else:
            if progress:
                progress(
                    f"WildRiftCore builds: {idx}/{total} — {champion_id} "
                    f"(из кэша {reused}, скачано {downloaded})"
                )
            try:
                html = _wildriftcore_get(net, build_url, progress).text
                payload = parse_wildriftcore_build_page(html, champion_id, known_items)
                if not payload.get("builds"):
                    raise RuntimeError("не найдена роль-специфичная сборка")
                downloaded += 1
                if _db is not None:
                    try:
                        _db.upsert_build_page_cache(
                            "wildriftcore.com", cache_patch, champion_id, payload, build_url
                        )
                        cached_pages[champion_id] = dict(payload)
                    except Exception:
                        pass
            except requests.HTTPError as exc:
                response = getattr(exc, "response", None)
                if response is not None and response.status_code == 429:
                    errors.append(
                        f"WildRiftCore builds: лимит запросов после {success}/{total} страниц (429)"
                    )
                    break
                errors.append(f"{champion_id}: {exc}")
                continue
            except Exception as exc:
                errors.append(f"{champion_id}: {exc}")
                continue

        if progress:
            progress(
                f"WildRiftCore builds: {idx}/{total} — {champion_id} "
                f"(из кэша {reused}, скачано {downloaded})"
            )
        success += 1
        source_url = str(payload.get("_source_url") or build_url)
        for row in payload.get("builds", []):
            role = str(row.get("role") or "")
            items = [canonical_item_name(x) for x in row.get("items", []) if canonical_item_name(x)]
            boot = canonical_item_name(str(row.get("boot") or ""))
            if role and items:
                builds_out.append((champion_id, role, items[:5], boot, cache_patch, source_url))
        for row in payload.get("situational", []):
            role = str(row.get("role") or "")
            item = canonical_item_name(str(row.get("item") or ""))
            if role and item:
                sit_out.append((
                    champion_id, role, item, str(row.get("trigger") or ""),
                    int(row.get("priority") or 999),
                ))
        for row in payload.get("boots", []):
            role = str(row.get("role") or "")
            item = canonical_item_name(str(row.get("item") or ""))
            if role and item:
                boots_out.append((
                    champion_id, role, item, str(row.get("trigger") or ""),
                    int(row.get("priority") or 999),
                ))

    if _db is not None and success == total:
        try:
            _db.prune_build_page_cache("wildriftcore.com", cache_patch)
        except Exception:
            pass

    if not builds_out:
        detail = "; ".join(errors[:3])
        raise RuntimeError(
            "WildRiftCore role builds не удалось загрузить"
            + (f" ({detail})" if detail else "")
        )
    return builds_out, sit_out, boots_out, errors


def _wildriftcore_item_slug(name: str) -> str:
    value = unicodedata.normalize("NFKD", clean_item_name(name)).encode("ascii", "ignore").decode("ascii")
    value = value.replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def parse_wildriftcore_item_page(html: str, url: str = "", name_hint: str = "") -> dict:
    """Parse a current source-native item that WR Pocket has not catalogued yet."""
    soup = BeautifulSoup(html or "", "html.parser")
    h1 = soup.find("h1")
    name = canonical_item_name(clean_item_name(h1.get_text(" ", strip=True)) if h1 else name_hint)
    if not name:
        name = canonical_item_name(name_hint)

    icon_url = ""
    wanted = slugish(name)
    for img in soup.find_all("img"):
        alt = clean_item_name(img.get("alt", ""))
        raw = str(img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "")
        if alt and slugish(alt) == wanted and raw:
            icon_url = urljoin(url or WR_CORE_CHAMPS, raw)
            break

    text = clean(soup.get_text(" ", strip=True))
    price = 0
    m = re.search(r"\bCost:\s*([0-9][0-9,]*)", text, re.I)
    if m:
        try:
            price = int(m.group(1).replace(",", ""))
        except ValueError:
            price = 0

    category = ""
    cm = re.search(r"\bCategory:\s*([A-Za-z][A-Za-z &/-]{1,40}?)(?=\s+Patch:|\s+Timing:|$)", text, re.I)
    if cm:
        category = clean(cm.group(1)).title()
    elif re.search(r"\bSupport\b", text):
        category = "Support"

    stats: list[str] = []
    heading = None
    for node in soup.find_all(["h2", "h3"]):
        if clean(node.get_text(" ", strip=True)).casefold() == "stats":
            heading = node
            break
    if heading is not None:
        for node in heading.find_all_next():
            if node is not heading and getattr(node, "name", None) in {"h2", "h3"}:
                break
            if getattr(node, "name", None) == "li":
                value = clean(node.get_text(" ", strip=True))
                if value and value not in stats:
                    stats.append(value)

    effect = ""
    summary_heading = None
    for node in soup.find_all(["h2", "h3"]):
        if clean(node.get_text(" ", strip=True)).casefold() == "strategic summary":
            summary_heading = node
            break
    if summary_heading is not None:
        paragraph = summary_heading.find_next("p")
        if paragraph is not None:
            effect = clean(paragraph.get_text(" ", strip=True))

    return {
        "name": name, "category": category, "tier": "Upgraded",
        "icon_url": icon_url, "detail_url": url, "price": price,
        "stats": stats, "effect_en": effect,
    }


def fetch_wildriftcore_item_metadata(
    net: Net,
    item_names: Iterable[str],
    progress: Callable[[str], None] | None = None,
) -> tuple[list[dict], list[str]]:
    """Fetch metadata only for WRC build items absent from the local item catalog."""
    names = list(dict.fromkeys(canonical_item_name(x) for x in item_names if canonical_item_name(x)))
    rows: list[dict] = []
    errors: list[str] = []
    total = len(names)
    for idx, name in enumerate(names, 1):
        slug = _wildriftcore_item_slug(name)
        if not slug:
            continue
        url = urljoin("https://wildriftcore.com/en/items/", slug + "/")
        if progress:
            progress(f"WildRiftCore items: {idx}/{total} — {name}")
        try:
            html = _wildriftcore_get(net, url, progress).text
            row = parse_wildriftcore_item_page(html, url, name)
            if not row.get("name"):
                raise RuntimeError("item name not found")
            rows.append(row)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return rows, errors


def _links(soup: BeautifulSoup, contains: str, base: str) -> list[tuple[str, str]]:
    seen = set()
    out = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if contains not in href:
            continue
        url = urljoin(base, href)
        if url in seen:
            continue
        seen.add(url)
        out.append((clean(a.get_text(" ", strip=True)), url))
    return out


def _is_generic_item_link_text(value: str) -> bool:
    text = clean(value).casefold()
    return not text or text.startswith("detail page") or text in {"details", "detail", "view", "open"}


def _item_card_for_link(a):
    """Return the nearest ancestor that looks like one complete item card.

    WR Pocket currently renders the item icon/title separately from the
    "Detail page" anchor, so looking only inside the anchor loses the icon.
    """
    href = a.get("href", "")
    current = a
    for _ in range(6):
        current = getattr(current, "parent", None)
        if current is None or not hasattr(current, "find_all"):
            break
        links = [
            x for x in current.find_all("a", href=True)
            if "/en/items/" in x.get("href", "")
        ]
        same_links = [x for x in links if x.get("href", "") == href]
        if same_links and current.find("img") is not None and len({x.get("href", "") for x in links}) == 1:
            return current
    return a.parent if a.parent is not None else a


def _raw_img_src(img) -> str:
    if img is None:
        return ""
    return str(img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "").strip()


def _is_equip_icon_url(value: str) -> bool:
    text = str(value or "").casefold()
    return "equipicons" in text or "/assets/img/items/" in text


def _trusted_item_card_image(card, detail_url: str, slug_key: str = ""):
    """Return an image only when it is demonstrably tied to this item card.

    The old code used card.find("img"), which can select recipe/similar-item
    artwork from the surrounding card. That caused valid item names to receive
    somebody else's icon. We now accept only an image inside the exact item's
    detail link or one whose alt text matches the item slug.
    """
    wanted_url = str(detail_url or "").rstrip("/")
    for link in card.find_all("a", href=True):
        linked = urljoin(WR_POCKET_ITEMS, str(link.get("href") or "")).rstrip("/")
        if linked != wanted_url:
            continue
        img = link.find("img")
        if img is not None and _raw_img_src(img):
            return img

    wanted = str(slug_key or "")
    if wanted:
        for img in card.find_all("img"):
            alt = clean_item_name(img.get("alt", ""))
            if alt and slugish(alt) == wanted and _raw_img_src(img):
                return img
    return None


def _main_item_image(soup, item_name: str = ""):
    """Pick the primary item portrait from a WR Pocket detail page.

    Detail pages place the main item image before the H1 title, while recipe
    component images appear much later under Recipe. Prefer an exact alt match;
    otherwise choose the last EquipIcons image before H1. This keeps recipe
    icons from being mistaken for the finished item.
    """
    wanted = slugish(clean_item_name(item_name)) if item_name else ""
    images = list(soup.find_all("img"))
    if wanted:
        for img in images:
            alt = clean_item_name(img.get("alt", ""))
            if alt and slugish(alt) == wanted and _raw_img_src(img):
                return img

    h1 = soup.find("h1")
    if h1 is not None:
        preceding = []
        for img in h1.find_all_previous("img"):
            raw = _raw_img_src(img)
            if raw and _is_equip_icon_url(raw):
                preceding.append(img)
        if preceding:
            # find_all_previous() is nearest-first.
            return preceding[0]

        for node in h1.find_all_next():
            if getattr(node, "name", None) in {"h2", "h3"}:
                title = clean(node.get_text(" ", strip=True)).casefold()
                if title == "recipe":
                    break
            if getattr(node, "name", None) == "img":
                raw = _raw_img_src(node)
                if raw and _is_equip_icon_url(raw):
                    return node

    for img in images:
        raw = _raw_img_src(img)
        if raw and "equipicons" in raw.casefold():
            return img
    return None


def _item_section_lines(card, start_label: str, stop_labels: set[str]) -> list[str]:
    """Extract a textual section from one WR Pocket item card.

    The site changes element nesting fairly often, but the visible labels
    ``Stats``, ``Effect`` and ``Recipe`` have remained stable. Parsing the
    flattened line stream makes the updater resilient to harmless DOM changes.
    """
    lines = [clean(x) for x in card.get_text("\n", strip=True).splitlines()]
    lines = [x for x in lines if x]
    start = -1
    wanted = start_label.casefold()
    for idx, line in enumerate(lines):
        if line.casefold() == wanted:
            start = idx + 1
            break
    if start < 0:
        return []
    out: list[str] = []
    stops = {x.casefold() for x in stop_labels}
    for line in lines[start:]:
        folded = line.casefold()
        if folded in stops or folded.startswith("detail page"):
            break
        out.append(line)
    return out


def _item_section_candidates(card, start_label: str, stop_labels: set[str]) -> list[list[str]]:
    """Return every visible section matching ``start_label``.

    WR Pocket detail pages currently contain both navigation/tab labels and the
    real content headings.  Looking only at the first ``Stats``/``Effect`` label
    can therefore select a UI stub instead of the actual item data.
    """
    lines = [clean(x) for x in card.get_text("\n", strip=True).splitlines()]
    lines = [x for x in lines if x]
    wanted = start_label.casefold()
    stops = {x.casefold() for x in stop_labels}
    sections: list[list[str]] = []
    for idx, line in enumerate(lines):
        if line.casefold() != wanted:
            continue
        out: list[str] = []
        for candidate in lines[idx + 1:]:
            folded = candidate.casefold()
            if folded in stops or folded.startswith("detail page"):
                break
            out.append(candidate)
        sections.append(out)
    return sections


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


def clean_wrpocket_item_stats(lines: Iterable[str] | str) -> list[str]:
    """Keep only real numeric item stats from WR Pocket text.

    The site can expose bilingual text and service tags in the same flattened
    DOM stream.  Tooltips must never persist labels such as ``Physical``,
    ``Upgraded`` or ``Gold Eff`` as if they were statistics.
    """
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
        line = clean(raw)
        if not line:
            continue
        # Passive descriptions sometimes contain numeric scaling.  They are not
        # base stats and should remain in the Effect section.
        if effect_words.search(line) and not line.lstrip().startswith("+"):
            continue
        for match in _ITEM_STAT_RE.finditer(line):
            value = clean(match.group(0))
            value = re.sub(r"^\+\s+", "+", value)
            key = value.casefold()
            if key not in seen:
                seen.add(key)
                out.append(value)
    return out


_ITEM_EFFECT_STOP = re.compile(
    r"\b(?:Recipe|Builds Into|Similar Items|Build Trends|Detail page|Gold Eff)\b",
    flags=re.I,
)


def clean_wrpocket_item_effect(lines: Iterable[str] | str) -> str:
    """Normalize one passive/active effect without page chrome or item tags."""
    if isinstance(lines, str):
        raw_lines = [lines]
    else:
        raw_lines = [str(x or "") for x in (lines or [])]
    kept: list[str] = []
    for raw in raw_lines:
        line = clean(raw)
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
    # A malformed DOM selection should not turn an entire page into one tooltip.
    if len(text) > 1800:
        text = text[:1800].rsplit(" ", 1)[0].rstrip() + "…"
    return text


def _best_item_stats(card) -> list[str]:
    candidates = _item_section_candidates(card, "Stats", {"Effect", "Recipe"})
    cleaned = [clean_wrpocket_item_stats(section) for section in candidates]
    cleaned = [row for row in cleaned if row]
    return max(cleaned, key=len) if cleaned else []


def _best_item_effect(card) -> str:
    candidates = _item_section_candidates(card, "Effect", {"Recipe", "Stats"})
    cleaned = [clean_wrpocket_item_effect(section) for section in candidates]
    cleaned = [row for row in cleaned if row]
    # The real effect is generally the longest non-empty Effect section; tab
    # labels yield an empty/very short candidate.
    return max(cleaned, key=len) if cleaned else ""


def _extract_item_price(text: str) -> int:
    value = clean(text)
    match = re.search(r"\bPrice\s*([0-9][0-9,]{1,5})\s*G\b", value, re.I)
    if not match:
        match = re.search(r"\b([0-9][0-9,]{1,5})\s*G\b", value, re.I)
    if not match:
        return 0
    try:
        return int(match.group(1).replace(",", ""))
    except (TypeError, ValueError):
        return 0


ITEM_TIERS = ("Upgraded", "Mid-tier", "Basic", "Starter loadout")
ITEM_CATEGORIES = ("Physical", "Magic", "Defense", "Support", "Boots")


def _exact_card_label(card, values: tuple[str, ...]) -> str:
    if card is None:
        return ""
    wanted = {value.casefold(): value for value in values}
    try:
        strings = card.stripped_strings
    except AttributeError:
        strings = []
    for raw in strings:
        value = clean(str(raw))
        canonical = wanted.get(value.casefold())
        if canonical:
            return canonical
    return ""


def extract_wrpocket_item_tier(card) -> str:
    """Return WR Pocket's explicit shop tier for one catalog card."""
    return _exact_card_label(card, ITEM_TIERS)


def extract_wrpocket_item_category(card) -> str:
    """Return the catalog category without confusing effect text for metadata."""
    return _exact_card_label(card, ITEM_CATEGORIES)


def is_finished_item_tier(tier: str) -> bool:
    """Only fully upgraded shop items are legal final-build recommendations."""
    return clean(str(tier or "")).casefold() == "upgraded"


def _item_dataset_hash(name: str, price: int, stats: list[str], effect_en: str, icon_url: str = "", detail_url: str = "") -> str:
    payload = json.dumps(
        {"name": clean_item_name(name), "price": int(price or 0), "stats": stats, "effect_en": clean(effect_en), "icon_url": icon_url or "", "detail_url": detail_url or ""},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_wrpocket_item_dataset_html(html: str, wanted_names: Iterable[str] | None = None) -> list[dict]:
    """Parse current item characteristics from the WR Pocket items index.

    Only one catalog page is needed for all items. This keeps subsequent update
    checks cheap and avoids opening every item detail page again.
    """
    soup = BeautifulSoup(html, "html.parser")
    wanted = None
    if wanted_names is not None:
        wanted = {slugish(clean_item_name(x).replace("’", "'")) for x in wanted_names if clean_item_name(x)}
    rows: list[dict] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/en/items/" not in href:
            continue
        detail_url = urljoin(WR_POCKET_ITEMS, href)
        if detail_url.rstrip("/") == WR_POCKET_ITEMS.rstrip("/"):
            continue
        card = _item_card_for_link(a)
        if card is None:
            continue

        # The item card contains passive/effect headings (for example "Gale")
        # alongside the real item name.  Trust the detail-page slug as the card
        # identity and only accept a visible candidate that matches it.
        slug = detail_url.rstrip("/").rsplit("/", 1)[-1]
        slug_key = slugish(slug)
        name = ""
        candidates: list[str] = []
        for tag_name in ("h1", "h2", "h3", "h4", "h5", "strong", "b"):
            for tag in card.find_all(tag_name):
                candidate = clean_item_name(tag.get_text(" ", strip=True))
                if candidate and len(candidate) <= 80 and not _is_generic_item_link_text(candidate):
                    candidates.append(candidate)
        img = _trusted_item_card_image(card, detail_url, slug_key)
        if img:
            candidate = clean_item_name(img.get("alt", ""))
            if candidate and len(candidate) <= 80:
                candidates.append(candidate)
        for candidate in candidates:
            if slugish(candidate) == slug_key:
                name = candidate
                break
        if not name:
            name = clean_item_name(slug.replace("-", " ").title())
        name = canonical_item_name(name)
        key = slugish(name.replace("’", "'"))
        if not key or key in seen:
            continue
        if wanted is not None and key not in wanted and not any(key.startswith(w) or w.startswith(key) for w in wanted):
            continue

        card_text = clean(card.get_text(" ", strip=True))
        price = _extract_item_price(card_text)
        stats = _best_item_stats(card)
        effect_en = _best_item_effect(card)
        category = extract_wrpocket_item_category(card)
        tier = extract_wrpocket_item_tier(card)

        icon_url = ""
        if img is not None:
            raw = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            if raw:
                icon_url = urljoin(WR_POCKET_ITEMS, raw)
        seen.add(key)
        rows.append({
            "name": name,
            "category": category,
            "tier": tier,
            "icon_url": icon_url,
            "detail_url": detail_url,
            "price": price,
            "stats": stats,
            "effect_en": effect_en,
            "data_hash": _item_dataset_hash(name, price, stats, effect_en, icon_url, detail_url),
        })
    return rows




def parse_wrpocket_item_detail_html(html: str, url: str = "", name_hint: str = "") -> dict:
    """Parse one WR Pocket item detail page into the local characteristics schema.

    This is deliberately independent from the catalog-card parser so it can act
    as a fallback when WR Pocket changes the layout of /en/items.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    name = ""
    h1 = soup.find("h1")
    if h1 is not None:
        name = clean_item_name(h1.get_text(" ", strip=True))
    if not name:
        name = clean_item_name(name_hint)
    if not name and url:
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        name = clean_item_name(slug.replace("-", " ").title())
    name = canonical_item_name(name)

    text = clean(soup.get_text(" ", strip=True))
    price = _extract_item_price(text)
    stats = _best_item_stats(soup)
    effect_en = _best_item_effect(soup)

    icon_url = ""
    chosen = _main_item_image(soup, name)
    if chosen is not None:
        raw = _raw_img_src(chosen)
        if raw:
            icon_url = urljoin(url or WR_POCKET_ITEMS, raw)

    return {
        "name": name,
        "category": extract_wrpocket_item_category(soup),
        "tier": extract_wrpocket_item_tier(soup),
        "icon_url": icon_url,
        "detail_url": url,
        "price": price,
        "stats": stats,
        "effect_en": effect_en,
        "data_hash": _item_dataset_hash(name, price, stats, effect_en, icon_url, url) if name else "",
    }


def _item_has_current_details(row: dict | None, patch: str) -> bool:
    if not row:
        return False
    try:
        stats = json.loads(row.get("stats_json") or "[]") if isinstance(row.get("stats_json"), str) else list(row.get("stats_json") or [])
    except Exception:
        stats = []
    has_details = bool(int(row.get("price") or 0) > 0 or stats or str(row.get("effect_en") or "").strip())
    if not has_details:
        return False
    return not patch or str(row.get("data_patch") or "") == str(patch)


def item_detail_fallback_names(
    wanted_names: Iterable[str], parsed_rows: Iterable[dict], existing_by_slug: dict[str, dict], patch: str,
    force_refresh: bool = False,
) -> list[str]:
    """Return only wanted items that still need a detail-page fetch."""
    parsed = {slugish(clean_item_name(row.get("name", ""))) for row in parsed_rows if clean_item_name(row.get("name", ""))}
    out: list[str] = []
    for raw in sorted({clean_item_name(x) for x in wanted_names if clean_item_name(x)}, key=str.casefold):
        key = slugish(raw)
        if key in parsed:
            continue
        if force_refresh:
            out.append(raw)
            continue
        row = existing_by_slug.get(key) or existing_by_slug.get(raw.casefold())
        if _item_has_current_details(row, patch):
            continue
        out.append(raw)
    return out


def fetch_wrpocket_item_detail_dataset(
    net: Net, wanted_names: Iterable[str], progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Fetch detail pages only for the requested missing item names."""
    wanted = [clean_item_name(x) for x in wanted_names if clean_item_name(x)]
    if not wanted:
        return []
    index_html = net.get(WR_POCKET_ITEMS).text
    soup = BeautifulSoup(index_html, "html.parser")
    by_slug: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/en/items/" not in href:
            continue
        detail_url = urljoin(WR_POCKET_ITEMS, href)
        if detail_url.rstrip("/") == WR_POCKET_ITEMS.rstrip("/"):
            continue
        href_slug = slugish(detail_url.rstrip("/").rsplit("/", 1)[-1])
        if href_slug:
            by_slug.setdefault(href_slug, detail_url)

    rows: list[dict] = []
    total = len(wanted)
    for idx, name in enumerate(wanted, 1):
        key = slugish(name)
        url = by_slug.get(key)
        if not url:
            # WR Pocket uses predictable item slugs; this fallback also covers a
            # catalog markup change that hides detail anchors from the index.
            slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").casefold()).strip("-")
            url = f"https://wrpocket.app/en/items/{slug}"
        if progress:
            progress(f"Wild Rift Pocket details: {idx}/{total} — {name}")
        try:
            html = net.get(url).text
            row = parse_wrpocket_item_detail_html(html, url, name)
        except Exception:
            continue
        if not row.get("name"):
            continue
        # A detail page that contains no actual characteristics is not considered
        # a successful fallback; keeping old data is safer.
        if not (row.get("price") or row.get("stats") or row.get("effect_en")):
            continue
        rows.append(row)
    return rows


@dataclass(frozen=True)
class ItemDatasetFetch:
    status_code: int
    rows: list[dict]
    etag: str = ""
    last_modified: str = ""
    dataset_hash: str = ""


def verify_wrpocket_item_icons(
    net: Net,
    records: Iterable[dict],
    progress: Callable[[str], None] | None = None,
) -> list[dict]:
    """Resolve final-item icons from the item's own detail page only.

    The catalog index is allowed to supply names/stats/categories, but never the
    final icon authority: recipe and "similar item" artwork can sit inside the
    same surrounding DOM. Each finished item therefore receives an explicit
    verification marker. If its detail page cannot prove the icon, icon_url is
    blanked for this refresh so an unverified catalog image cannot overwrite a
    previously known-good URL in SQLite.
    """
    rows = [dict(row) for row in records]
    targets = [
        row for row in rows
        if is_finished_item_tier(str(row.get("tier") or ""))
    ]
    total = len(targets)
    for idx, row in enumerate(targets, 1):
        row["_icon_verified"] = False
        row["_icon_verification_error"] = ""
        # Never trust the catalog-card image as the final authority.
        row["icon_url"] = ""
        detail_url = str(row.get("detail_url") or "")
        if progress:
            progress(f"Wild Rift Pocket icons: {idx}/{total} — {row.get('name') or ''}")
        if not detail_url:
            row["_icon_verification_error"] = "нет detail URL"
            continue
        try:
            detail_name, icon_url = _fetch_wrpocket_item_detail(net, detail_url)
        except Exception as exc:
            row["_icon_verification_error"] = str(exc)
            continue
        expected = slugish(canonical_item_name(str(row.get("name") or "")))
        actual = slugish(canonical_item_name(detail_name))
        if expected and actual and expected != actual:
            row["_icon_verification_error"] = f"страница другого предмета: {detail_name}"
            continue
        if not icon_url or not _is_equip_icon_url(icon_url):
            row["_icon_verification_error"] = "точная EquipIcons-ссылка не найдена"
            continue
        row["icon_url"] = icon_url
        row["_icon_verified"] = True
        row["data_hash"] = _item_dataset_hash(
            str(row.get("name") or ""),
            int(row.get("price") or 0),
            list(row.get("stats") or []),
            str(row.get("effect_en") or ""),
            icon_url,
            detail_url,
        )
    return rows


def fetch_wrpocket_item_dataset(
    net: Net, wanted_names: Iterable[str] | None = None, conditional_headers: dict | None = None,
) -> ItemDatasetFetch:
    """Fetch the item dataset once, honoring HTTP conditional validators."""
    response = net.get(WR_POCKET_ITEMS, headers=conditional_headers or None, allow_not_modified=True)
    status = int(getattr(response, "status_code", 200) or 200)
    headers = getattr(response, "headers", {}) or {}
    header_map = {str(k).casefold(): str(v) for k, v in headers.items()}
    if status == 304:
        return ItemDatasetFetch(304, [], header_map.get("etag", ""), header_map.get("last-modified", ""), "")
    rows = parse_wrpocket_item_dataset_html(getattr(response, "text", "") or "", wanted_names)
    dataset_payload = "\n".join(f"{slugish(row['name'])}:{row['data_hash']}" for row in sorted(rows, key=lambda r: slugish(r["name"])))
    dataset_hash = hashlib.sha256(dataset_payload.encode("utf-8")).hexdigest() if rows else ""
    return ItemDatasetFetch(
        status, rows, header_map.get("etag", ""), header_map.get("last-modified", ""), dataset_hash,
    )


def _fetch_wrpocket_item_detail(net: Net, url: str) -> tuple[str, str]:
    """Read canonical item name/icon from the detail page as a layout fallback."""
    soup = BeautifulSoup(net.get(url).text, "html.parser")
    h1 = soup.find("h1")
    name = clean(h1.get_text(" ", strip=True)) if h1 else ""
    chosen = _main_item_image(soup, name)
    icon_url = ""
    if chosen is not None:
        raw = _raw_img_src(chosen)
        if raw:
            icon_url = urljoin(url, raw)
    return name, icon_url


def _patch_version_key(value: str) -> tuple[int, int, int]:
    """Sort Wild Rift patch versions numerically, preserving letter suffix order."""
    m = re.fullmatch(r"\s*([0-9]+)\.([0-9]+)([a-z]?)\s*", value or "", re.I)
    if not m:
        return (-1, -1, -1)
    suffix = m.group(3).lower()
    return int(m.group(1)), int(m.group(2)), (ord(suffix) - ord("a") + 1 if suffix else 0)


def _published_date_from_html(html: str) -> str:
    """Extract YYYY-MM-DD from a Riot article page."""
    soup = BeautifulSoup(html, "html.parser")
    for meta in soup.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").casefold()
        if key in {"article:published_time", "datepublished", "date", "pubdate"}:
            raw = meta.get("content") or ""
            dm = re.search(r"(20[0-9]{2}-[0-9]{2}-[0-9]{2})", raw)
            if dm:
                return dm.group(1)
    for time_node in soup.find_all("time"):
        raw = time_node.get("datetime") or clean(time_node.get_text(" ", strip=True))
        dm = re.search(r"(20[0-9]{2}-[0-9]{2}-[0-9]{2})", raw or "")
        if dm:
            return dm.group(1)
    dm = re.search(r'"datePublished"\s*:\s*"(20[0-9]{2}-[0-9]{2}-[0-9]{2})', html, re.I)
    if dm:
        return dm.group(1)
    dm = re.search(r"(20[0-9]{2}-[0-9]{2}-[0-9]{2})T[0-9:.+-Z]+", html)
    return dm.group(1) if dm else ""


def fetch_riot_patch_info(net: Net) -> tuple[str, str]:
    """Return the newest official Riot Wild Rift patch and its publication date.

    Riot's tag page HTML is not guaranteed to list cards in DOM order, so we
    select the greatest semantic patch (7.2e > 7.2d > 7.2) instead of taking
    the first matching link.
    """
    html = net.get(RIOT_PATCH_NOTES).text
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(r"Wild\s+Rift\s+Patch\s+Notes\s+([0-9]+\.[0-9]+[a-z]?)\b", re.I)

    candidates: list[tuple[str, str, str]] = []
    for a in soup.find_all("a", href=True):
        text = clean(a.get_text(" ", strip=True))
        m = pattern.search(text)
        if not m:
            continue
        patch = m.group(1)
        date = ""
        container = a.find_parent(["article", "li", "section", "div"])
        if container is not None:
            time_node = container.find("time")
            if time_node is not None:
                raw = time_node.get("datetime") or clean(time_node.get_text(" ", strip=True))
                dm = re.search(r"(20[0-9]{2}-[0-9]{2}-[0-9]{2})", raw or "")
                if dm:
                    date = dm.group(1)
        candidates.append((patch, urljoin(RIOT_PATCH_NOTES, a.get("href", "")), date))

    if candidates:
        patch, href, date = max(candidates, key=lambda row: _patch_version_key(row[0]))
        if href:
            try:
                detail_html = net.get(href).text
                detail_date = _published_date_from_html(detail_html)
                if detail_date:
                    date = detail_date
            except Exception:
                pass
        return patch, date

    # Fallback if Riot changes card markup but keeps patch titles in page text.
    versions = pattern.findall(clean(soup.get_text(" ", strip=True)))
    if not versions:
        raise RuntimeError("Official Riot patch version not found")
    patch = max(versions, key=_patch_version_key)
    return patch, _published_date_from_html(html)

def fetch_current_patch_info(net: Net) -> tuple[str, str]:
    """Prefer Riot's official patch notes; fall back to Wild Rift Pocket."""
    try:
        patch, date = fetch_riot_patch_info(net)
        if patch:
            return patch, date
    except Exception:
        pass
    return fetch_wrpocket_patch_info(net)


def fetch_wrpocket_patch_info(net: Net) -> tuple[str, str]:
    """Return (patch, source date), preserving letter suffixes such as 7.2e."""
    html = net.get(WR_POCKET_PATCH).text
    text = clean(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))
    m = re.search(r"\bLatest\s+([0-9]+\.[0-9]+[a-z]?)\b", text, re.I)
    if not m:
        m = re.search(r"\bPatch\s+([0-9]+\.[0-9]+[a-z]?)\b", text, re.I)
    patch = m.group(1) if m else ""
    dm = re.search(r"\bupdated\s+(20[0-9]{2}-[0-9]{2}-[0-9]{2})\b", text, re.I)
    return patch, (dm.group(1) if dm else "")


def fetch_wrpocket_patch_version(net: Net) -> str:
    """Compatibility wrapper returning the complete patch name (e.g. 7.2e)."""
    return fetch_wrpocket_patch_info(net)[0]


def _fetch_wrpocket_items_from_index(net: Net) -> list[tuple[str, str, str]]:
    """Compatibility/index parser used when no Build Trends filter is supplied."""
    soup = BeautifulSoup(net.get(WR_POCKET_ITEMS).text, "html.parser")
    rows = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/en/items/" not in href:
            continue
        card = _item_card_for_link(a)
        img = a.find("img") or (card.find("img") if card is not None else None)
        anchor_text = clean_item_name(a.get_text(" ", strip=True))
        name = "" if _is_generic_item_link_text(anchor_text) else anchor_text
        if not name and card is not None:
            for tag_name in ("h1", "h2", "h3", "h4", "h5", "strong", "b"):
                for tag in card.find_all(tag_name):
                    candidate = clean_item_name(tag.get_text(" ", strip=True))
                    if candidate and len(candidate) <= 80 and not _is_generic_item_link_text(candidate):
                        name = candidate
                        break
                if name:
                    break
        if not name and img:
            candidate = clean_item_name(img.get("alt", ""))
            if candidate and len(candidate) <= 80 and re.search(r"[A-Za-z]", candidate):
                name = candidate
        icon_url = ""
        if img:
            raw = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
            if raw:
                icon_url = urljoin(WR_POCKET_ITEMS, raw)
        detail_url = urljoin(WR_POCKET_ITEMS, href)
        if not name or not icon_url:
            try:
                detail_name, detail_icon = _fetch_wrpocket_item_detail(net, detail_url)
                name = name or detail_name
                icon_url = icon_url or detail_icon
            except Exception:
                pass
        if not name:
            continue
        key = name.casefold().replace("’", "'")
        if key in seen:
            continue
        seen.add(key)
        rows.append((name, "", icon_url))
    return rows


def fetch_wrpocket_items(net: Net, wanted_names: Iterable[str] | None = None) -> list[tuple[str, str, str]]:
    """Resolve real item records from WR Pocket detail pages.

    The updater supplies the names from champion Build Trends. In that mode we
    treat each item detail page as authoritative and discard everything that is
    not part of those ready-made builds. Calling without a filter keeps the
    compact index parser for tests/backwards compatibility.
    """
    if wanted_names is None:
        return _fetch_wrpocket_items_from_index(net)
    wanted = {slugish(clean_item_name(x).replace("’", "'")) for x in wanted_names if clean_item_name(x)}
    soup = BeautifulSoup(net.get(WR_POCKET_ITEMS).text, "html.parser")
    detail_urls: list[str] = []
    seen_urls: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/en/items/" not in href:
            continue
        url = urljoin(WR_POCKET_ITEMS, href)
        if url.rstrip("/") == WR_POCKET_ITEMS.rstrip("/") or url in seen_urls:
            continue
        href_key = slugish(url.rstrip("/").rsplit("/", 1)[-1])
        if wanted and href_key and href_key not in wanted and not any(href_key.startswith(w) or w.startswith(href_key) for w in wanted):
            continue
        seen_urls.add(url)
        detail_urls.append(url)

    rows: list[tuple[str, str, str]] = []
    seen_names: set[str] = set()
    for url in detail_urls:
        try:
            name, icon_url = _fetch_wrpocket_item_detail(net, url)
        except Exception:
            continue
        name = clean_item_name(name)
        if not name or len(name) > 80:
            continue
        key = slugish(name.replace("’", "'"))
        if wanted and key not in wanted:
            if not any(key == w or key.startswith(w) or w.startswith(key) for w in wanted):
                continue
        folded = name.casefold().replace("’", "'")
        if folded in seen_names:
            continue
        seen_names.add(folded)
        rows.append((name, "", icon_url))
    return rows


def fetch_wrpocket_item_pools(net: Net, resolve: Callable[[str], str | None], progress: Callable[[str], None] | None = None) -> list[tuple[str, str, str, int]]:
    soup = BeautifulSoup(net.get(WR_POCKET_CHAMPS).text, "html.parser")
    links = _links(soup, "/en/champions/", WR_POCKET_CHAMPS)
    filtered = []
    for text, url in links:
        if url.rstrip("/") == WR_POCKET_CHAMPS.rstrip("/"):
            continue
        if url.count("/champions/") != 1:
            continue
        filtered.append((text, url))
    rows = []
    visited = set()
    resolved_urls: dict[str, str] = {}
    resolved_count = 0
    for idx, (text, url) in enumerate(filtered, 1):
        if url in visited:
            continue
        visited.add(url)

        # The profile URL is the canonical champion identity. Never resolve the
        # whole visible card text first: WR Pocket cards can contain tier/score
        # decorations and neighboring text. A contaminated card string can still
        # fuzzy-resolve to a valid champion (the last one used to be Zyra), which
        # would then attach many champions' item pools to that one champion.
        slug_raw = url.rstrip("/").split("/")[-1]
        slug_guess = slug_raw.replace("-", " ")
        # A profile URL is already a canonical identity. Never fuzzy-resolve it
        # through display names: an upstream name mismatch once made "norra"
        # resolve to Zyra and collapsed two profiles into one champion.
        exact_resolve = getattr(resolve, "exact_id", resolve)
        cid = exact_resolve(slug_guess)
        if not cid:
            if progress:
                progress(
                    f"Wild Rift Pocket: {idx}/{len(filtered)} — "
                    f"неизвестный профиль {slug_raw}, пропущен"
                )
            continue

        previous_url = resolved_urls.get(cid)
        if previous_url and previous_url != url:
            raise RuntimeError(
                f"WR Pocket item pools: разные profile URL разрешились в одного чемпиона {cid}: "
                f"{previous_url} / {url}"
            )
        resolved_urls[cid] = url
        resolved_count += 1

        if progress:
            progress(f"Wild Rift Pocket: {idx}/{len(filtered)} — {cid}")
        psoup = BeautifulSoup(net.get(url).text, "html.parser")
        heading = None
        for h in psoup.find_all(["h2", "h3", "h4"]):
            if clean(h.get_text(" ", strip=True)).casefold() == "items":
                heading = h
                break
        if heading is None:
            continue
        category = ""
        priority = 0
        for el in heading.find_all_next():
            if el is heading:
                continue
            if el.name in ("h2", "h3") and clean(el.get_text(" ", strip=True)).casefold() in {"runes", "spells", "available skins", "skills", "stats"}:
                break
            if el.name in ("h4", "h5"):
                category = clean(el.get_text(" ", strip=True))
                continue
            if el.name == "a" and "/items/" in el.get("href", ""):
                item = clean_item_name(el.get_text(" ", strip=True))
                if item:
                    priority += 1
                    rows.append((cid, item, category, priority))
            elif el.name == "li" and not el.find("a"):
                item = clean_item_name(el.get_text(" ", strip=True))
                if item and 2 <= len(item) <= 55:
                    priority += 1
                    rows.append((cid, item, category, priority))
    # Refuse to publish a suspiciously incomplete/collapsed refresh. The caller
    # keeps the previous known-good pool when this raises.
    unique_profiles = len(resolved_urls)
    expected_profiles = len({url for _text, url in filtered})
    min_profiles = min(expected_profiles, max(20, int(expected_profiles * 0.70)))
    if expected_profiles and unique_profiles < min_profiles:
        raise RuntimeError(
            f"WR Pocket item pools: разрешено только {unique_profiles}/{expected_profiles} чемпионов; "
            "старый пул сохранён"
        )

    # de-duplicate while preserving first position
    dedup = {}
    for row in rows:
        key = (row[0], row[1].casefold())
        if key not in dedup:
            dedup[key] = row
    return list(dedup.values())


WR_ITEM_RU_OVERRIDES = {
    "Thornmail": "Шипованный доспех",
    "Mortal Reminder": "Глашатай смерти",
    "Morellonomicon": "Мореллономикон",
    "Chempunk Chainsword": "Химпанковый цепной меч",
    "Randuin's Omen": "Знамение Рандуина",
    "Frozen Heart": "Ледяное сердце",
    "Plated Steelcaps": "Бронированные сапоги",
    "Armored Advance": "Тяжелое наступление",
    "Mercury's Treads": "Поступь Меркурия",
    "Force of Nature": "Сила природы",
    "Spirit Visage": "Облачение духов",
    "Maw of Malmortius": "Зев Малмортиуса",
    "Banshee's Veil": "Завеса банши",
    "Quicksilver Enchant": "Зачарование ртути",
    "Stasis Enchant": "Зачарование стазиса",
    "Guardian Angel": "Ангел-хранитель",
    "Sterak's Gage": "Испытание Стерака",
    "Edge of Night": "Грань ночи",
    "Serpent's Fang": "Змеиный клык",
    "Blade of the Ruined King": "Клинок Падшего короля",
    "Black Cleaver": "Черная секира",
    "Serylda's Grudge": "Злоба Серильды",
    "Dominik's Regards": "Поклон лорда Доминика",
    "Dominik’s Regards": "Поклон лорда Доминика",
    "Terminus": "Светотень",
    "Liandry's Torment": "Мучения Лиандри",
    "Void Staff": "Посох Бездны",
    "Cryptbloom": "Могильный цветок",
    "Chainlaced Crushers": "Цепные крушители",
    "Armorcrusher Boots": "Ботинки крушителя брони",
    "Immortal Treads": "Бессмертные шаги",
    "Crimson Lucidity": "Алое просветление",
    "Gunmetal Greaves": "Наголенники из орудийной стали",
    "Divine Sunderer": "Божественный раскалыватель",
    "Experimental Hexplate": "Экспериментальная хекстековая броня",
    "Kraken Slayer": "Убийца кракенов",
    "Knight's Vow": "Клятва рыцаря",
    "Guinsoo's Rageblade": "Клинок ярости Гинсу",
    "Runaan's Hurricane": "Ураган Рунаан",
    "Duskblade of Draktharr": "Сумеречный клинок Драктарра",
    "Death's Dance": "Танец смерти",
    "Infinity Orb": "Сфера бесконечности",
    "Sunfire Aegis": "Эгида солнечного пламени",
    "Redemption": "Искупление",
    "Harmonic Echo": "Мелодичное эхо",
    "Radiant Virtue": "Сияющая добродетель",
    "Dawnshroud": "Утренний покров",
    "Titanic Hydra": "Титаническая гидра",
    "Ionian Boots of Lucidity": "Ионийские сапоги просветления",
    "Boots of Dynamism": "Ботинки динамичности",
    "Gluttonous Greaves": "Алчные наголенники",
    "Long Sword": "Длинный меч",
    "Brawler's Gloves": "Перчатки драчуна",
    "Dagger": "Кинжал",
    "Tear of the Goddess": "Слеза богини",
    "Ruby Crystal": "Рубиновый кристалл",
    "Cloth Armor": "Тканевая броня",
    "Null-Magic Mantle": "Магический плащ",
    "Executioner's Calling": "Зов палача",
    "Last Whisper": "Предсмертный шепот",
    "Cloak of Agility": "Плащ ловкости",
    "Abyssal Mask": "Маска Бездны",
    "Amaranth's Twinguard": "Двойная защита Амаранта",
    "Archangel's Staff": "Посох архангела",
    "Ardent Censer": "Пылающая кадильница",
    "Blackfire Torch": "Факел темного огня",
    "Bloodletter's Curse": "Проклятие кровопускателя",
    "Bloodthirster": "Кровопийца",
    "Cosmic Drive": "Космический ускоритель",
    "Dead Man's Plate": "Броня мертвеца",
    "Dusk and Dawn": "Сумерки и рассвет",
    "Eclipse": "Затмение",
    "Essence Reaver": "Похититель сущности",
    "Galeforce": "Сила шторма",
    "Gargoyle Stoneplate": "Каменный доспех гаргульи",
    "Goredrinker": "Потрошитель",
    "Heartsteel": "Сердце стали",
    "Hextech Rocketbelt": "Хекстековый ракетный ремень",
    "Hollow Radiance": "Сияние пустоты",
    "Horizon Focus": "Фокус горизонта",
    "Hullbreaker": "Корпусолом",
    "Iceborn Gauntlet": "Хладорожденная рукавица",
    "Immortal Boots": "Бессмертные сапоги",
    "Imperial Mandate": "Имперский мандат",
    "Infinity Edge": "Грань Бесконечности",
    "Kaenic Rookern": "Кэниковый рукэрн",
    "Lich Bane": "Гроза Личей",
    "Locket of the Iron Solari": "Медальон Железных Солари",
    "Luden's Echo": "Эхо Людена",
    "Magnetic Blaster": "Магнитный бластер",
    "Malignance": "Пагубность",
    "Manamune": "Манамунэ",
    "Mantle of the Twelfth Hour": "Мантия двенадцатого часа",
    "Mercurial Scimitar": "Ртутный ятаган",
    "Mikael's Blessing": "Благословение Микаэля",
    "Nashor's Tooth": "Зуб Нашора",
    "Navori Quickblades": "Быстрые клинки Навори",
    "Oceanid's Trident": "Трезубец океанида",
    "Overlord's Bloodmail": "Кровавая кольчуга владыки",
    "Phantom Dancer": "Призрачный танцор",
    "Rabadon's Deathcap": "Смертельная шляпа Рабадона",
    "Relic Shield": "Реликтовый щит",
    "Riftmaker": "Творец разломов",
    "Rod of Ages": "Жезл веков",
    "Rylai's Crystal Scepter": "Хрустальный скипетр Рилай",
    "Searing Crown": "Пылающая корона",
    "Shurelya's Battlesong": "Боевая песнь Шурелии",
    "Soul Transfer": "Перенос души",
    "Spear of Shojin": "Копье Сёдзина",
    "Spectral Sickle": "Призрачный серп",
    "Spellslinger's Shoes": "Сапоги заклинателя",
    "Staff of Flowing Water": "Посох текущей воды",
    "Stormsurge": "Штормовой прилив",
    "Stridebreaker": "Костолом",
    "Sundered Sky": "Расколотое небо",
    "The Collector": "Коллектор",
    "Trinity Force": "Тройственный Союз",
    "Unending Despair": "Бесконечное отчаяние",
    "Warmog's Armor": "Доспехи Вармога",
    "Wit's End": "Клинок ярости Вита",
    "Yordle Trap": "Йордловая ловушка",
    "Youmuu's Ghostblade": "Призрачный клинок Йомуу",
    "Zeke's Convergence": "Конвергенция Зика",
    "Zhonya's Hourglass": "Песочные часы Жони",
}


def canonical_item_name(name: str) -> str:
    """Map punctuation/case variants from scraped Build Trends to one canonical WR name."""
    value = clean_item_name(name)
    if not value:
        return ""
    wanted = slugish(value)
    for canonical in WR_ITEM_RU_OVERRIDES:
        if slugish(canonical) == wanted:
            return canonical
    return value


def canonical_completed_item_name(name: str) -> str:
    """Backward-compatible alias: current patch boot names are already final."""
    return canonical_item_name(name)


def _public_item_asset_slug(name: str) -> str:
    """Stable WildRiftMeta/RiftGG asset slug from a canonical item name.

    Possessive apostrophes are removed, not converted into separators:
    "Mercury's Treads" -> "mercurys-treads",
    "Randuin's Omen" -> "randuins-omen".
    """
    value = canonical_item_name(name).replace("’", "").replace("'", "")
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").casefold()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def trusted_item_icon_urls(name: str) -> list[str]:
    """Independent WR icon mirrors keyed by the item's canonical English name.

    Do not derive final icons from WR Pocket card/detail DOM: some current pages
    expose generic/PC artwork under a valid Wild Rift item name. These two icon
    endpoints are name-addressed; media_cache still validates that the response
    is an actual image before it is accepted.
    """
    slug = _public_item_asset_slug(name)
    if not slug:
        return []
    return [
        f"https://www.wildriftmeta.com/assets/item/icon/item-{slug}-icon.png",
        f"https://assets.riftgg.app/items/{slug}.webp",
    ]


def item_name_ru(name: str, ddragon_map: dict[str, str] | None = None) -> str:
    normalized = canonical_item_name(name).replace("’", "'")
    for key, value in WR_ITEM_RU_OVERRIDES.items():
        if key.replace("’", "'").casefold() == normalized.casefold():
            return value
    if ddragon_map:
        return ddragon_map.get(normalized.casefold(), "")
    return ""


def fetch_counter_item_pages(net: Net, resolve: Callable[[str], str | None], known_items: Iterable[str], progress: Callable[[str], None] | None = None) -> list[tuple[str, str, str]]:
    known = sorted({clean(x) for x in known_items if clean(x)}, key=len, reverse=True)
    soup = BeautifulSoup(_wildriftcounter_get(net, WR_COUNTER_CHAMPS, progress).text, "html.parser")
    links = _links(soup, "/champions/", WR_COUNTER_CHAMPS)
    out = []
    visited = set()
    for idx, (text, url) in enumerate(links, 1):
        if url in visited or url.rstrip("/") == WR_COUNTER_CHAMPS.rstrip("/"):
            continue
        visited.add(url)
        slug = url.rstrip("/").split("/")[-1].replace("-", " ")
        exact_resolve = getattr(resolve, "exact_id", resolve)
        cid = exact_resolve(slug) or resolve(text)
        if not cid:
            continue
        if progress:
            progress(f"WildRiftCounter items: {idx}/{len(links)} — {text or slug}")
        psoup = BeautifulSoup(_wildriftcounter_get(net, url, progress).text, "html.parser")
        start = None
        for h in psoup.find_all(["h2", "h3"]):
            txt = clean(h.get_text(" ", strip=True)).casefold()
            if txt.startswith("item counter") or "items that counter" in txt:
                start = h
                break
        if start is None:
            continue
        chunks = []
        for el in start.find_all_next():
            if el is start:
                continue
            if el.name == "h2":
                break
            if el.name in ("p", "li", "a"):
                chunks.append(clean(el.get_text(" ", strip=True)))
            if el.name == "img" and el.get("alt"):
                chunks.append(clean(el.get("alt")))
        segment = "\n".join(chunks)
        segfold = segment.casefold().replace("’", "'")
        found = set()
        for item in known:
            if item.casefold().replace("’", "'") in segfold:
                found.add(item)
        for item in sorted(found):
            out.append((cid, item, f"Counter item против {cid}"))
    return out
