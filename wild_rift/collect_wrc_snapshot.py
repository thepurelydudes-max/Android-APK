from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import sources

OUT = Path("wrc_snapshot")
RAW_BUILDS = OUT / "raw" / "builds"
RAW_COUNTERS = OUT / "raw" / "counters"
RAW_HTML_BUILDS = OUT / "raw_html" / "builds"
RAW_HTML_COUNTERS = OUT / "raw_html" / "counters"


def _slug_links(text: str) -> list[str]:
    patterns = [
        r"https?://(?:www\.)?wildriftcore\.com/en/champions/([^/?#)\s]+)/builds/?",
        r"/en/champions/([^/?#)\s]+)/builds/?",
    ]
    found: list[str] = []
    for pattern in patterns:
        for slug in re.findall(pattern, text or "", flags=re.I):
            slug = str(slug).strip().strip("/")
            if slug and slug not in found:
                found.append(slug)
    return sorted(found)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW_BUILDS.mkdir(parents=True, exist_ok=True)
    RAW_COUNTERS.mkdir(parents=True, exist_ok=True)
    RAW_HTML_BUILDS.mkdir(parents=True, exist_ok=True)
    RAW_HTML_COUNTERS.mkdir(parents=True, exist_ok=True)

    net = sources.Net()
    print("Discovering WildRiftCore champion build pages...", flush=True)
    index_text, index_transport = sources._wildriftcore_build_text(
        net, sources.WR_CORE_BUILDS, print
    )
    _write_text(OUT / "raw" / "builds_index.txt", index_text)
    slugs = _slug_links(index_text)
    if not slugs:
        raise RuntimeError("No WildRiftCore champion build links discovered")

    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "https://wildriftcore.com/en/builds/",
        "index_transport": index_transport,
        "champions_discovered": len(slugs),
        "champions": [],
        "errors": [],
    }

    for i, slug in enumerate(slugs, 1):
        build_url = f"https://wildriftcore.com/en/champions/{slug}/builds/"
        counters_url = f"https://wildriftcore.com/en/champions/{slug}/counters/"
        print(f"[{i}/{len(slugs)}] {slug}", flush=True)
        row = {
            "slug": slug,
            "build_url": build_url,
            "counters_url": counters_url,
            "build": {},
            "counters": {},
        }

        try:
            # Reader/Markdown is the canonical analysis snapshot because it
            # preserves headings and labels across Cloudflare/SSR variations.
            text = sources._jina_reader_get(net, build_url, print).text
            _write_text(RAW_BUILDS / f"{slug}.md", text)
            row["build"] = {
                "ok": True,
                "transport": "reader",
                "bytes": len(text.encode("utf-8")),
                "sha256": _sha256(text),
                "has_when_to_pick": "when to pick it" in text.casefold(),
                "has_example_enemy_draft": "example enemy draft" in text.casefold(),
                "has_situational_adaptations": "situational adaptations" in text.casefold(),
                "has_adaptations_by_opponent": "adaptations by opponent" in text.casefold(),
            }
            # Preserve direct HTML as a second raw representation when WRC
            # allows it. Failure here never invalidates the reader snapshot.
            try:
                direct = sources._wildriftcore_get(
                    net, build_url, print, sources.WR_CORE_HTML_HEADERS
                ).text
                _write_text(RAW_HTML_BUILDS / f"{slug}.html", direct)
                row["build"]["direct_html"] = {
                    "ok": True,
                    "bytes": len(direct.encode("utf-8")),
                    "sha256": _sha256(direct),
                }
            except Exception as direct_exc:
                row["build"]["direct_html"] = {
                    "ok": False, "error": str(direct_exc)
                }
        except Exception as exc:
            row["build"] = {"ok": False, "error": str(exc)}
            manifest["errors"].append(f"{slug} builds: {exc}")

        try:
            text = sources._jina_reader_get(net, counters_url, print).text
            _write_text(RAW_COUNTERS / f"{slug}.md", text)
            row["counters"] = {
                "ok": True,
                "transport": "reader",
                "bytes": len(text.encode("utf-8")),
                "sha256": _sha256(text),
                "has_edge": "edge" in text.casefold(),
                "has_best_picks": "best picks" in text.casefold(),
                "has_do_not_pick": "do not pick" in text.casefold(),
                "has_key_item": "key item" in text.casefold(),
            }
            try:
                direct = sources._wildriftcore_get(
                    net, counters_url, print, sources.WR_CORE_HTML_HEADERS
                ).text
                _write_text(RAW_HTML_COUNTERS / f"{slug}.html", direct)
                row["counters"]["direct_html"] = {
                    "ok": True,
                    "bytes": len(direct.encode("utf-8")),
                    "sha256": _sha256(direct),
                }
            except Exception as direct_exc:
                row["counters"]["direct_html"] = {
                    "ok": False, "error": str(direct_exc)
                }
        except Exception as exc:
            row["counters"] = {"ok": False, "error": str(exc)}
            manifest["errors"].append(f"{slug} counters: {exc}")

        manifest["champions"].append(row)

    builds_ok = sum(1 for row in manifest["champions"] if row["build"].get("ok"))
    counters_ok = sum(1 for row in manifest["champions"] if row["counters"].get("ok"))
    manifest["coverage"] = {
        "builds_ok": builds_ok,
        "builds_total": len(slugs),
        "counters_ok": counters_ok,
        "counters_total": len(slugs),
    }

    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest["coverage"], ensure_ascii=False, indent=2), flush=True)

    if builds_ok != len(slugs):
        raise RuntimeError(
            f"Incomplete WRC build snapshot: {builds_ok}/{len(slugs)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
