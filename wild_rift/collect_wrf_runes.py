from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://www.wildriftfire.com"
SOURCE = "wildriftfire.com"
ROLE_MAP = {
    "solo": "Барон",
    "baron": "Барон",
    "jungle": "Лес",
    "mid": "Мид",
    "duo": "ADC",
    "adc": "ADC",
    "support": "Саппорт",
}

EXPLICIT_CHAMPION_ALIASES = {
    "nunu and willump": "nunu",
    "nunu willump": "nunu",
}


def norm(value: str) -> str:
    text = str(value or "").casefold().replace("&", " and ")
    text = text.replace("’", "'")
    text = re.sub(r"[^a-z0-9а-яё]+", " ", text)
    return " ".join(text.split())


def slugify(value: str) -> str:
    text = norm(value).replace(" and ", " ")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


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
                time.sleep(1.0 + attempt)
    raise RuntimeError(f"GET failed {url}: {last}")


def champion_index(con: sqlite3.Connection):
    rows = con.execute("SELECT id,name,name_ru FROM champions").fetchall()
    by_norm: dict[str, str] = {}
    champion_ids = set()
    for cid, name, name_ru in rows:
        cid = str(cid)
        champion_ids.add(cid)
        for value in (cid, name, name_ru):
            key = norm(value)
            if key:
                by_norm.setdefault(key, cid)
    for alias, canonical in EXPLICIT_CHAMPION_ALIASES.items():
        key = norm(canonical)
        if key in by_norm:
            by_norm[norm(alias)] = by_norm[key]
    return by_norm, champion_ids


def resolve_champion(by_norm: dict[str, str], display: str, href: str) -> str | None:
    candidates = [
        display,
        href.rstrip("/").split("/")[-1].replace("-", " "),
    ]
    for raw in candidates:
        key = norm(raw)
        if key in EXPLICIT_CHAMPION_ALIASES:
            key = norm(EXPLICIT_CHAMPION_ALIASES[key])
        if key in by_norm:
            return by_norm[key]
    return None


def parse_patch(soup: BeautifulSoup) -> str:
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    m = re.search(r"Patch\s+([0-9]+(?:\.[0-9]+)*(?:[a-z])?)", title, re.I)
    if m:
        return m.group(1)
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Patch\s+([0-9]+(?:\.[0-9]+)*(?:[a-z])?)", text, re.I)
    return m.group(1) if m else ""


def parse_role_labels(soup: BeautifulSoup) -> list[tuple[str, str]]:
    labels: list[tuple[str, str]] = []
    for span in soup.select(".wf-champion__guide-selector span"):
        text = " ".join(span.get_text(" ", strip=True).split())
        m = re.match(r"^(Solo|Baron|Jungle|Mid|Duo|ADC|Support)\s+Build\b", text, re.I)
        if not m:
            continue
        source_role = m.group(1).casefold()
        role = ROLE_MAP.get(source_role)
        if role:
            labels.append((role, text))
    return labels


def rune_rows(block, session: requests.Session, cache_root: Path, patch: str) -> list[dict]:
    section = block.select_one(".section.runes")
    if not section:
        return []
    rows = []
    for holder in section.select(".ico-holder"):
        name_el = holder.select_one(".name")
        name = " ".join((name_el.get_text(" ", strip=True) if name_el else holder.get_text(" ", strip=True)).split())
        img = holder.find("img")
        rel = ""
        if img:
            rel = str(img.get("src") or img.get("data-src") or "").strip()
        if not name:
            continue
        icon_url = urljoin(BASE, rel) if rel else ""
        icon_path = ""
        if icon_url:
            parsed = urlparse(icon_url)
            basename = Path(parsed.path).name or (slugify(name) + ".png")
            target = cache_root / "runes" / basename
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file() or target.stat().st_size < 100:
                rr = fetch(session, icon_url)
                target.write_bytes(rr.content)
            icon_path = f"cache/runes/{target.name}"
        rows.append({
            "name": name,
            "icon_url": icon_url,
            "icon_path": icon_path,
            "patch": patch,
        })
    return rows


def situational_rows(block) -> list[dict]:
    out = []
    if not block:
        return out
    for order, section in enumerate(block.select(".section.situation"), 1):
        cond = section.select_one("span.situation")
        condition = " ".join(cond.get_text(" ", strip=True).split()) if cond else ""
        holders = section.select(".ico-holder")
        names = []
        for holder in holders:
            name_el = holder.select_one(".name")
            name = " ".join((name_el.get_text(" ", strip=True) if name_el else holder.get_text(" ", strip=True)).split())
            if name:
                names.append(name)
        if len(names) >= 2:
            out.append({
                "condition": condition,
                "from_rune": names[0],
                "to_rune": names[1],
                "priority": order,
            })
    return out


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS runes (
            name TEXT PRIMARY KEY,
            name_ru TEXT NOT NULL DEFAULT '',
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
            source TEXT NOT NULL,
            patch TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (champion_id, role, source),
            FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS role_rune_adaptations (
            champion_id TEXT NOT NULL,
            role TEXT NOT NULL,
            condition_text TEXT NOT NULL DEFAULT '',
            from_rune TEXT NOT NULL,
            to_rune TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 999,
            source TEXT NOT NULL,
            patch TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (champion_id, role, condition_text, from_rune, to_rune, source),
            FOREIGN KEY (champion_id) REFERENCES champions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_role_runes_lookup
            ON role_runes(champion_id,role,source);
        CREATE INDEX IF NOT EXISTS idx_role_rune_adapt_lookup
            ON role_rune_adaptations(champion_id,role,source,priority);
        """
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--audit-json", default="")
    ap.add_argument("--expected-patch", default="")
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
        by_norm, champion_ids = champion_index(con)

        root = BeautifulSoup(fetch(session, BASE + "/").text, "html.parser")
        guide_links = []
        seen = set()
        for a in root.select('a[href*="/guide/"]'):
            href = str(a.get("href") or "").strip()
            if not href.startswith("/guide/") or href in seen:
                continue
            seen.add(href)
            display = " ".join(a.get_text(" ", strip=True).split())
            guide_links.append((href, display))

        collected: list[dict] = []
        unresolved_guides: list[dict] = []
        malformed_pages: list[dict] = []
        patches: dict[str, int] = {}

        # Replace this source atomically after collection succeeds.
        staged_role_runes = []
        staged_adaptations = []
        rune_catalog: dict[str, dict] = {}

        for idx, (href, display) in enumerate(guide_links, 1):
            cid = resolve_champion(by_norm, display, href)
            if not cid:
                unresolved_guides.append({"display": display, "href": href})
                continue
            url = urljoin(BASE, href)
            soup = BeautifulSoup(fetch(session, url).text, "html.parser")
            patch = parse_patch(soup)
            patches[patch] = patches.get(patch, 0) + 1
            if args.expected_patch and patch and patch != args.expected_patch:
                malformed_pages.append({
                    "champion_id": cid, "url": url,
                    "error": f"patch {patch} != {args.expected_patch}",
                })
                continue

            labels = parse_role_labels(soup)
            spell_blocks = soup.select(".wf-champion__data__spells")
            sit_blocks = soup.select(".wf-champion__data__situational-runes")
            if len(labels) != len(spell_blocks):
                malformed_pages.append({
                    "champion_id": cid, "url": url,
                    "error": f"role labels {len(labels)} != spell blocks {len(spell_blocks)}",
                    "labels": labels,
                })
                continue

            for role_index, ((role, label), block) in enumerate(zip(labels, spell_blocks)):
                rows = rune_rows(block, session, cache_root, patch)
                if len(rows) != 5:
                    malformed_pages.append({
                        "champion_id": cid, "role": role, "url": url,
                        "error": f"expected 5 runes, got {len(rows)}",
                        "runes": [r["name"] for r in rows],
                    })
                    continue
                for row in rows:
                    rune_catalog[row["name"]] = row
                staged_role_runes.append({
                    "champion_id": cid,
                    "role": role,
                    "runes": [r["name"] for r in rows],
                    "patch": patch,
                    "source_url": url,
                })
                sit = sit_blocks[role_index] if role_index < len(sit_blocks) else None
                for adaptation in situational_rows(sit):
                    staged_adaptations.append({
                        "champion_id": cid,
                        "role": role,
                        **adaptation,
                        "patch": patch,
                        "source_url": url,
                    })
                collected.append({
                    "champion_id": cid,
                    "role": role,
                    "runes": [r["name"] for r in rows],
                    "situational": situational_rows(sit),
                    "source_url": url,
                    "label": label,
                    "patch": patch,
                })
            print(f"WildRiftFire runes {idx}/{len(guide_links)}: {display} -> {cid} ({len(labels)} roles)")

        if malformed_pages:
            # Do not publish a partial source refresh silently.
            print("MALFORMED SAMPLE", json.dumps(malformed_pages[:20], ensure_ascii=False, indent=2))

        con.execute("DELETE FROM role_rune_adaptations WHERE source=?", (SOURCE,))
        con.execute("DELETE FROM role_runes WHERE source=?", (SOURCE,))
        con.execute("DELETE FROM runes WHERE source=?", (SOURCE,))
        for name, row in rune_catalog.items():
            con.execute(
                """INSERT INTO runes(name,icon_url,icon_path,source,patch,updated_at)
                   VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(name) DO UPDATE SET
                     icon_url=excluded.icon_url,
                     icon_path=excluded.icon_path,
                     source=excluded.source,
                     patch=excluded.patch,
                     updated_at=CURRENT_TIMESTAMP""",
                (name, row["icon_url"], row["icon_path"], SOURCE, row["patch"]),
            )
        for row in staged_role_runes:
            con.execute(
                """INSERT INTO role_runes(champion_id,role,runes_json,source,patch,source_url,updated_at)
                   VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    row["champion_id"], row["role"],
                    json.dumps(row["runes"], ensure_ascii=False),
                    SOURCE, row["patch"], row["source_url"],
                ),
            )
        for row in staged_adaptations:
            con.execute(
                """INSERT INTO role_rune_adaptations(
                     champion_id,role,condition_text,from_rune,to_rune,priority,
                     source,patch,source_url,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    row["champion_id"], row["role"], row["condition"],
                    row["from_rune"], row["to_rune"], row["priority"],
                    SOURCE, row["patch"], row["source_url"],
                ),
            )

        role_build_pairs = {
            (str(cid), str(role))
            for cid, role in con.execute(
                "SELECT DISTINCT champion_id,role FROM role_builds WHERE source='wildriftcore.com'"
            )
        }
        rune_pairs = {
            (str(row["champion_id"]), str(row["role"]))
            for row in staged_role_runes
        }
        missing_for_recommendations = sorted(role_build_pairs - rune_pairs)
        extra_rune_pairs = sorted(rune_pairs - role_build_pairs)

        audit = {
            "source": SOURCE,
            "guide_links": len(guide_links),
            "database_champions": len(champion_ids),
            "resolved_guides": len({row["champion_id"] for row in collected}),
            "unresolved_guides": unresolved_guides,
            "role_pages": len(staged_role_runes),
            "unique_runes": len(rune_catalog),
            "situational_rules": len(staged_adaptations),
            "patches": patches,
            "malformed_pages": malformed_pages,
            "role_build_pairs": len(role_build_pairs),
            "covered_recommendation_pairs": len(role_build_pairs & rune_pairs),
            "missing_recommendation_pairs": [
                {"champion_id": cid, "role": role}
                for cid, role in missing_for_recommendations
            ],
            "extra_rune_pairs": [
                {"champion_id": cid, "role": role}
                for cid, role in extra_rune_pairs
            ],
            "rune_icons": len(list((cache_root / "runes").glob("*.png"))),
        }
        con.commit()

    print("RUNE_AUDIT", json.dumps(audit, ensure_ascii=False, indent=2))
    if args.audit_json:
        Path(args.audit_json).write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    # Data collection itself succeeds even if source coverage differs from WRC.
    # The caller decides whether missing recommendation pairs are acceptable.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
