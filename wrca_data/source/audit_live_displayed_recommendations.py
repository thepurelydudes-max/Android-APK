"""Read-only source-matched audit of WRCA's displayed rune recommendations.

Only compares a WR Pocket primary-lane 5-rune pick order with the stored WR
Pocket primary-lane recommendation. Other sources or roles are never treated
as identical. Reports coverage failures explicitly. Does not write a DB.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://wrpocket.app"
SRC = "wrpocket.app:tencent-cn"
LANES = {"top": "Барон", "jungle": "Лес", "mid": "Мид", "adc": "ADC", "support": "Саппорт"}


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    return re.sub(r"[^a-z0-9]", "", value)


def extract(url: str) -> dict:
    try:
        r = requests.get(url, timeout=22, headers={"User-Agent": "WRCA-ReadOnly-Audit/1.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        heading = next(
            (h for h in soup.find_all("h3") if
             h.parent and "wrp-cnb-group" in (h.parent.get("class") or []) and
             h.get_text(" ", strip=True).strip().startswith("Runes")),
            None,
        )
        primary = next(
            (b for b in soup.select("button[data-cnb-lane]")
             if b.get("aria-pressed") == "true"), None)
        if not heading or not primary:
            return {"url": url, "error": "primary lane or rune-combo list absent"}
        role = LANES.get(primary.get("data-cnb-lane", ""))
        table = heading.parent.select_one("ol.wrp-cnb-tbl")
        rows = table.select("li.wrp-cnb-row") if table else []
        first = next((r for r in rows if r.get("data-ar") == "1"), None)
        if not first:
            first = next((r for r in rows if "wrp-cnb-head" not in (r.get("class") or [])), None)
        names = [b.get("data-name-en", "").strip() for b in (first.select("button[data-name-en]") if first else [])]
        # WRP uses a lowercase variant for the same official rune identity.
        names = [{"axiom arcanist": "Axiom Arcanist"}.get(name.casefold(), name) for name in names]
        if not role or len(names) != 5 or len(set(names)) != 5:
            return {"url": url, "error": f"invalid live lane/runes: {role} {names!r}"}
        return {"url": url, "role": role, "runes": names}
    except Exception as exc:
        return {"url": url, "error": str(exc)[:250]}


def audit(db: Path, *, workers: int) -> dict:
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        champs = [dict(r) for r in con.execute("SELECT id,name FROM champions")]
        old = {(r["champion_id"], r["role"]): json.loads(r["runes_json"])
               for r in con.execute("SELECT champion_id,role,runes_json FROM role_runes WHERE source=?", (SRC,))}
        valid_runes = {r[0] for r in con.execute("SELECT name FROM runes")}
    response = requests.get(BASE + "/en/champions", timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    urls = {}
    for a in soup.select("a[href^='/en/champions/']"):
        href = a.get("href", "").split("?")[0].rstrip("/")
        slug = href.rsplit("/", 1)[-1]
        for value in (slug, a.get_text(" ", strip=True)):
            key = norm(value)
            if key:
                urls.setdefault(key, BASE + href)
    # These are canonical game IDs / Riot names that diverge from site slugs.
    substitutions = {
        "monkeyking": "wukong", "jarvaniv": "jarvaniv", "drmundo": "drmundo",
        "missfortune": "missfortune", "chogath": "chogath",
        "kogmaw": "kogmaw", "ksante": "ksante",
    }
    links = {}
    missing = []
    for c in champs:
        candidate = substitutions.get(norm(c["id"]), norm(c["id"]))
        url = urls.get(candidate) or urls.get(norm(c["name"]))
        if not url:
            missing.append({"champion": c["id"], "name": c["name"]})
        else:
            links[c["id"]] = url
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {executor.submit(extract, url): cid for cid, url in links.items()}
        for fut in concurrent.futures.as_completed(future_to_id):
            cid = future_to_id[fut]
            data = fut.result()
            data["champion"] = cid
            results.append(data)
    changes = []
    same = []
    uncovered = []
    for row in sorted(results, key=lambda r: r["champion"]):
        if row.get("error"):
            uncovered.append(row)
            continue
        pair = (row["champion"], row["role"])
        current = old.get(pair)
        if current is None:
            uncovered.append({**row, "error": "No same-source baseline for primary role"})
            continue
        if any(name not in valid_runes for name in row["runes"]):
            uncovered.append({**row, "error": "Source contains rune absent from verified catalog"})
            continue
        if current == row["runes"]:
            same.append({"champion": pair[0], "role": pair[1]})
        else:
            changes.append({**row, "old": current})
    return {
        "source": SRC,
        "baseline_champions": len(champs),
        "baseline_wr_pocket_role_runes": len(old),
        "discovered_urls": len(links),
        "missing_champion_urls": missing,
        "successfully_compared_primary_roles": len(same) + len(changes),
        "unchanged_primary_roles": len(same),
        "changed_primary_roles": len(changes),
        "unchanged": same,
        "changes": changes,
        "failed_or_uncovered": uncovered,
        "other_roles_not_compared": len(old) - len(same) - len(changes),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()
    report = audit(args.db, workers=args.workers)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("WRCA_RUNE_AUDIT_SUMMARY", json.dumps({
        key: value for key, value in report.items()
        if key not in {"changes", "unchanged", "failed_or_uncovered"}
    }, ensure_ascii=False), flush=True)
    for row in report["changes"]:
        print("WRCA_RUNE_CHANGE", json.dumps({
            "champion": row["champion"], "role": row["role"],
            "old": row["old"], "new": row["runes"]
        }, ensure_ascii=False), flush=True)
    for row in report["failed_or_uncovered"][:20]:
        print("WRCA_RUNE_UNCOVERED", json.dumps(row, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
