from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://wrpocket.app"
SOURCE = "wrpocket.app:tencent-cn"
BAND = "diamond"
LANE_TO_ROLE = {
    "top": "Барон",
    "baron": "Барон",
    "jungle": "Лес",
    "mid": "Мид",
    "adc": "ADC",
    "dragon": "ADC",
    "support": "Саппорт",
}

ALIASES = {
    "nunu and willump": "nunu",
    "nunu willump": "nunu",
    "kai sa": "kaisa",
    "k sante": "ksante",
    "cho gath": "chogath",
    "jarvan iv": "jarvaniv",
    "lee sin": "leesin",
    "master yi": "masteryi",
    "miss fortune": "missfortune",
    "twisted fate": "twistedfate",
    "xin zhao": "xinzhao",
    "aurelion sol": "aurelionsol",
    "dr mundo": "drmundo",
    "kha zix": "khazix",
    "rek sai": "reksai",
}


def norm(value: str) -> str:
    text = str(value or "").casefold().replace("’", "'").replace("&", " and ")
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text)
    return " ".join(text.split())


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", norm(value))


def pct(value: str) -> float | None:
    text = str(value or "").strip().replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def fetch(session: requests.Session, url: str, tries: int = 3) -> requests.Response:
    last = None
    for attempt in range(tries):
        try:
            r = session.get(url, timeout=(15, 45))
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            if attempt + 1 < tries:
                time.sleep(0.8 + attempt)
    raise RuntimeError(f"GET failed {url}: {last}")


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS runes (
            name TEXT PRIMARY KEY,
            name_ru TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            effect_en TEXT NOT NULL DEFAULT '',
            icon_url TEXT NOT NULL DEFAULT '',
            icon_path TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            patch TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS role_runes (
            champion_id TEXT NOT NULL,
            role TEXT NOT NULL,
            runes_json TEXT NOT NULL DEFAULT '[]',
            pick_rate REAL,
            win_rate REAL,
            rank_band TEXT NOT NULL DEFAULT '',
            sample_date TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            patch TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (champion_id, role, source),
            FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS role_rune_variants (
            champion_id TEXT NOT NULL,
            role TEXT NOT NULL,
            variant_rank INTEGER NOT NULL,
            runes_json TEXT NOT NULL DEFAULT '[]',
            pick_rate REAL,
            win_rate REAL,
            rank_band TEXT NOT NULL DEFAULT '',
            sample_date TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL,
            patch TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (champion_id, role, variant_rank, source),
            FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_role_runes_lookup
            ON role_runes(champion_id,role,source);
        CREATE INDEX IF NOT EXISTS idx_role_rune_variants_lookup
            ON role_rune_variants(champion_id,role,source,variant_rank);
        """
    )


def db_champion_index(con: sqlite3.Connection) -> tuple[dict[str, str], set[str]]:
    rows = con.execute("SELECT id,name,name_ru FROM champions").fetchall()
    index: dict[str, str] = {}
    ids: set[str] = set()
    for cid, name, name_ru in rows:
        cid = str(cid)
        ids.add(cid)
        for value in (cid, name, name_ru):
            if not value:
                continue
            index.setdefault(norm(value), cid)
            index.setdefault(compact(value), cid)
    # common source spellings
    for alias, target in ALIASES.items():
        target_key = compact(target)
        if target_key in index:
            index[norm(alias)] = index[target_key]
            index[compact(alias)] = index[target_key]
    return index, ids


def resolve_champion(index: dict[str, str], display: str, href: str) -> str | None:
    slug = href.rstrip("/").split("/")[-1]
    for value in (display, slug.replace("-", " "), slug):
        for key in (norm(value), compact(value)):
            mapped = ALIASES.get(key, ALIASES.get(norm(value), value))
            for k2 in (norm(mapped), compact(mapped)):
                if k2 in index:
                    return index[k2]
            if key in index:
                return index[key]
    return None


def parse_patch(soup: BeautifulSoup) -> str:
    # The page carries patch in asset versions and meta widgets.
    html = str(soup)
    matches = re.findall(r"(?:patch|v=)([0-9]+\.[0-9]+[a-z]?)", html, flags=re.I)
    for value in matches:
        if re.fullmatch(r"[0-9]+\.[0-9]+[a-z]?", value):
            return value
    meta = soup.select_one(".patch-pill-ver")
    return meta.get_text(" ", strip=True) if meta else ""


def page_catalog(session: requests.Session) -> list[tuple[str, str]]:
    soup = BeautifulSoup(fetch(session, BASE + "/en/champions").text, "html.parser")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.select('a[href^="/en/champions/"]'):
        href = str(a.get("href") or "").split("#", 1)[0].rstrip("/")
        if href.count("/") != 3 or href in seen:
            continue
        seen.add(href)
        display = " ".join(a.get_text(" ", strip=True).split())
        out.append((href, display))
    return out


def parse_template(template: Tag) -> BeautifulSoup:
    # BeautifulSoup does not expose text inside <template> through get_text()
    # consistently, but decode_contents() preserves the baked markup exactly.
    return BeautifulSoup(template.decode_contents(), "html.parser")


def source_date_for(root: Tag | BeautifulSoup, key: str) -> str:
    node = root.select_one(f'.wrp-cnb-src[data-cnb-for="{key}"]')
    if not node:
        return ""
    date = node.select_one(".wrp-cnb-dl")
    if date:
        return date.get_text(" ", strip=True)
    m = re.search(r"\d{4}-\d{2}-\d{2}", node.get_text(" ", strip=True))
    return m.group(0) if m else ""


def find_panel(soup: BeautifulSoup, key: str) -> tuple[Tag | None, Tag | BeautifulSoup | None]:
    panel = soup.select_one(f'.wrp-cnb-panel[data-cnb-for="{key}"]')
    if panel:
        # Active panel has actual text; placeholder panels are empty.
        if panel.select_one(".wrp-cnb-sr") and any(
            x.get_text(" ", strip=True) for x in panel.select(".wrp-cnb-sr")
        ):
            return panel, soup
    tpl = soup.select_one(f'template[data-cnb-tpl="{key}"]')
    if tpl:
        inner = parse_template(tpl)
        panel = inner.select_one(".wrp-cnb-panel")
        if panel:
            return panel, inner
    return None, None


def group_rows(group: Tag) -> list[dict]:
    rows: list[dict] = []
    for row in group.select("ol.wrp-cnb-tbl > li.wrp-cnb-row:not(.wrp-cnb-head)"):
        names = [
            " ".join(x.get_text(" ", strip=True).split())
            for x in row.select(".wrp-cnb-icons .wrp-cnb-sr")
        ]
        names = [x for x in names if x]
        if not names:
            continue
        pick_node = row.select_one(".wrp-cnb-bar")
        win_node = row.select_one(".wrp-cnb-wr")
        rows.append({
            "names": names,
            "pick_rate": pct(pick_node.get_text(" ", strip=True) if pick_node else ""),
            "win_rate": pct(win_node.get_text(" ", strip=True) if win_node else ""),
            "appear_rank": int(row.get("data-ar") or 999),
            "win_rank": int(row.get("data-wr") or 999),
            "row": row,
        })
    rows.sort(key=lambda x: (x["appear_rank"], -(x["pick_rate"] or -1.0)))
    return rows


def rune_rows_from_panel(panel: Tag) -> list[dict]:
    # Build Trends always uses 3-item / 5-rune / 2-spell combination groups.
    for group in panel.select(".wrp-cnb-group"):
        rows = group_rows(group)
        if rows and len(rows[0]["names"]) == 5:
            # Reject a malformed mixed group instead of silently storing junk.
            valid = [row for row in rows if len(row["names"]) == 5]
            return valid
    return []


def rune_asset_from_row(name: str, row: Tag) -> tuple[str, str]:
    key = norm(name)
    for a in row.select('a[href*="/runes/"]'):
        label = " ".join(a.get_text(" ", strip=True).split())
        sr = a.select_one(".wrp-cnb-sr")
        if sr:
            label = " ".join(sr.get_text(" ", strip=True).split())
        if norm(label) != key:
            continue
        href = urljoin(BASE, str(a.get("href") or ""))
        img = a.find("img")
        icon = ""
        if img:
            icon = str(
                img.get("src") or img.get("data-src") or
                img.get("data-lazy-src") or ""
            ).strip()
        return href, urljoin(BASE, icon) if icon else ""
    return "", ""


def enrich_rune_catalog(session: requests.Session, catalog: dict[str, dict], cache_root: Path) -> None:
    # The dedicated rune index provides the complete catalog (currently 52).
    soup = BeautifulSoup(fetch(session, BASE + "/en/runes").text, "html.parser")
    current_category = ""
    for node in soup.find_all(["h2", "h3", "h4", "a"]):
        if node.name in {"h2", "h3", "h4"}:
            text = " ".join(node.get_text(" ", strip=True).split())
            if text:
                current_category = text
            continue
        href = str(node.get("href") or "")
        if "/en/runes/" not in href:
            continue
        name = " ".join(node.get_text(" ", strip=True).split())
        # Prefer explicit SR/label when card has extra prose.
        sr = node.select_one(".wrp-cnb-sr, .name, .rune-name")
        if sr:
            name = " ".join(sr.get_text(" ", strip=True).split())
        if not name:
            slug = href.rstrip("/").split("/")[-1]
            name = slug.replace("-", " ").title()
        # If the card includes effect text, remove it by matching known combo name.
        known = next((k for k in catalog if norm(k) and norm(k) in norm(name)), "")
        if known:
            name = known
        row = catalog.setdefault(name, {
            "name": name, "category": "", "effect_en": "",
            "icon_url": "", "detail_url": urljoin(BASE, href),
        })
        if current_category and not row.get("category"):
            row["category"] = current_category
        row["detail_url"] = urljoin(BASE, href)
        img = node.find("img")
        if img and not row.get("icon_url"):
            raw = str(img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "")
            if raw:
                row["icon_url"] = urljoin(BASE, raw)

    rune_dir = cache_root / "runes"
    rune_dir.mkdir(parents=True, exist_ok=True)
    for name, row in catalog.items():
        icon_url = str(row.get("icon_url") or "")
        if not icon_url:
            continue
        suffix = Path(icon_url.split("?", 1)[0]).suffix or ".webp"
        safe = re.sub(r"[^a-z0-9]+", "_", compact(name)) or "rune"
        target = rune_dir / f"{safe}{suffix}"
        if not target.is_file() or target.stat().st_size < 100:
            try:
                target.write_bytes(fetch(session, icon_url).content)
            except Exception:
                continue
        row["icon_path"] = f"cache/runes/{target.name}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--audit-json", default="")
    ap.add_argument("--expected-patch", default="7.3a")
    args = ap.parse_args()

    db_path = Path(args.db).resolve()
    cache_root = Path(args.cache).resolve()
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; WRCA-RuneCollector/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    })

    with sqlite3.connect(db_path) as con:
        con.execute("PRAGMA foreign_keys=ON")
        ensure_schema(con)
        index, db_ids = db_champion_index(con)
        pages = page_catalog(session)

        base_rows: list[dict] = []
        variants: list[dict] = []
        rune_catalog: dict[str, dict] = {}
        unresolved: list[dict] = []
        page_errors: list[dict] = []
        source_lanes: dict[str, list[str]] = {}
        patches: dict[str, int] = {}

        for idx, (href, display) in enumerate(pages, 1):
            cid = resolve_champion(index, display, href)
            if not cid:
                unresolved.append({"href": href, "display": display})
                continue
            url = urljoin(BASE, href)
            try:
                soup = BeautifulSoup(fetch(session, url).text, "html.parser")
            except Exception as exc:
                page_errors.append({"champion_id": cid, "url": url, "error": str(exc)})
                continue
            patch = parse_patch(soup) or args.expected_patch
            patches[patch] = patches.get(patch, 0) + 1
            if args.expected_patch and patch and patch != args.expected_patch:
                page_errors.append({
                    "champion_id": cid, "url": url,
                    "error": f"patch mismatch {patch} != {args.expected_patch}",
                })
                continue

            lanes = [
                str(b.get("data-cnb-lane") or "").strip().casefold()
                for b in soup.select("[data-cnb-lane]")
                if str(b.get("data-cnb-lane") or "").strip()
            ]
            lanes = list(dict.fromkeys(lanes))
            source_lanes[cid] = lanes

            for lane in lanes:
                role = LANE_TO_ROLE.get(lane)
                if not role:
                    continue
                key = f"{BAND}:{lane}"
                panel, panel_root = find_panel(soup, key)
                if panel is None:
                    continue
                rows = rune_rows_from_panel(panel)
                if not rows:
                    continue
                sample_date = source_date_for(panel_root or soup, key)
                if not sample_date:
                    # Source line can live outside the template in rare layouts.
                    sample_date = source_date_for(soup, key)

                for rank, row in enumerate(rows, 1):
                    for name in row["names"]:
                        entry = rune_catalog.setdefault(name, {
                            "name": name, "category": "", "effect_en": "",
                            "icon_url": "", "detail_url": "",
                        })
                        detail, icon = rune_asset_from_row(name, row["row"])
                        if detail and not entry["detail_url"]:
                            entry["detail_url"] = detail
                        if icon and not entry["icon_url"]:
                            entry["icon_url"] = icon
                    variant = {
                        "champion_id": cid, "role": role,
                        "variant_rank": rank,
                        "runes": row["names"],
                        "pick_rate": row["pick_rate"],
                        "win_rate": row["win_rate"],
                        "rank_band": "Diamond+",
                        "sample_date": sample_date,
                        "patch": patch,
                        "source_url": url + "#trend-runes",
                    }
                    variants.append(variant)
                    if rank == 1:
                        base_rows.append(dict(variant))

            print(
                f"WR Pocket runes {idx}/{len(pages)}: {cid} "
                f"lanes={','.join(lanes) or '-'} base_total={len(base_rows)}"
            )

        enrich_rune_catalog(session, rune_catalog, cache_root)

        # Strict replacement of only our own source rows.
        con.execute("DELETE FROM role_rune_variants WHERE source=?", (SOURCE,))
        con.execute("DELETE FROM role_runes WHERE source=?", (SOURCE,))
        con.execute("DELETE FROM runes WHERE source=?", (SOURCE,))

        for name, row in rune_catalog.items():
            con.execute(
                """INSERT INTO runes(
                     name,category,effect_en,icon_url,icon_path,source,patch,updated_at
                   ) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(name) DO UPDATE SET
                     category=CASE WHEN excluded.category<>'' THEN excluded.category ELSE runes.category END,
                     effect_en=CASE WHEN excluded.effect_en<>'' THEN excluded.effect_en ELSE runes.effect_en END,
                     icon_url=CASE WHEN excluded.icon_url<>'' THEN excluded.icon_url ELSE runes.icon_url END,
                     icon_path=CASE WHEN excluded.icon_path<>'' THEN excluded.icon_path ELSE runes.icon_path END,
                     source=excluded.source,patch=excluded.patch,updated_at=CURRENT_TIMESTAMP""",
                (
                    name, str(row.get("category") or ""), str(row.get("effect_en") or ""),
                    str(row.get("icon_url") or ""), str(row.get("icon_path") or ""),
                    SOURCE, args.expected_patch,
                ),
            )
        for row in base_rows:
            con.execute(
                """INSERT INTO role_runes(
                     champion_id,role,runes_json,pick_rate,win_rate,rank_band,sample_date,
                     source,patch,source_url,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    row["champion_id"], row["role"],
                    json.dumps(row["runes"], ensure_ascii=False),
                    row["pick_rate"], row["win_rate"], row["rank_band"], row["sample_date"],
                    SOURCE, row["patch"], row["source_url"],
                ),
            )
        for row in variants:
            con.execute(
                """INSERT INTO role_rune_variants(
                     champion_id,role,variant_rank,runes_json,pick_rate,win_rate,
                     rank_band,sample_date,source,patch,source_url,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    row["champion_id"], row["role"], row["variant_rank"],
                    json.dumps(row["runes"], ensure_ascii=False),
                    row["pick_rate"], row["win_rate"], row["rank_band"], row["sample_date"],
                    SOURCE, row["patch"], row["source_url"],
                ),
            )

        role_build_pairs = {
            (str(cid), str(role))
            for cid, role in con.execute(
                "SELECT DISTINCT champion_id,role FROM role_builds WHERE source='wildriftcore.com'"
            )
        }
        rune_pairs = {(r["champion_id"], r["role"]) for r in base_rows}
        missing = sorted(role_build_pairs - rune_pairs)
        extra = sorted(rune_pairs - role_build_pairs)

        con.commit()
        quick = con.execute("PRAGMA quick_check").fetchone()[0]

    audit = {
        "source": SOURCE,
        "rank_band": "Diamond+",
        "pages_found": len(pages),
        "db_champions": len(db_ids),
        "unresolved_pages": unresolved,
        "page_errors": page_errors,
        "patches": patches,
        "role_rune_pages": len(base_rows),
        "role_rune_variants": len(variants),
        "unique_runes": len(rune_catalog),
        "rune_icons": len(list((cache_root / "runes").glob("*"))),
        "role_build_pairs": len(role_build_pairs),
        "covered_recommendation_pairs": len(role_build_pairs & rune_pairs),
        "missing_recommendation_pairs": [
            {"champion_id": cid, "role": role} for cid, role in missing
        ],
        "extra_rune_pairs": [
            {"champion_id": cid, "role": role} for cid, role in extra
        ],
        "source_lanes": source_lanes,
        "sqlite_quick_check": quick,
    }
    print("RUNE_AUDIT", json.dumps(audit, ensure_ascii=False, indent=2))
    if args.audit_json:
        Path(args.audit_json).write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
