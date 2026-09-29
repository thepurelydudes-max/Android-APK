from __future__ import annotations

from data_contract import MatchupEvidence, canonical_hero_id, rate_to_percent

MOONTON_GMS_SOURCE_URL = "https://api.gms.moontontech.com/api/gms/source/2669606/2756567"
MOONTON_GMS_SOURCE_NAME = "moonton.gms.2669606.2756567"


def _records(payload: dict) -> list[dict]:
    data = (payload or {}).get("data") or {}
    rows = data.get("records") or [] if isinstance(data, dict) else []
    return [row for row in rows if isinstance(row, dict)]


def parse_gms_source(payload: dict, *, rank_segment: str = "all") -> dict:
    """Parse one Moonton GMS source snapshot without UI/source-name coupling.

    The endpoint returns one main hero record with global WR/appearance/ban data
    plus two matchup lists. increase_win_rate is preserved as the raw fractional
    swing; normalization happens later across the whole matrix.
    """
    heroes: dict[str, dict] = {}
    stats: list[dict] = []
    evidence: dict[tuple[str, str], MatchupEvidence] = {}

    for record in _records(payload):
        row = record.get("data") if isinstance(record.get("data"), dict) else {}
        hero = row.get("main_hero") if isinstance(row.get("main_hero"), dict) else {}
        hero_data = hero.get("data") if isinstance(hero.get("data"), dict) else {}
        champion_id = canonical_hero_id(row.get("main_heroid"), hero_data.get("name"))
        if not champion_id:
            continue

        heroes[champion_id] = {
            "id": champion_id,
            "name": str(hero_data.get("name") or "").strip(),
            "icon_url": str(hero_data.get("head") or "").strip(),
        }
        stats.append({
            "champion_id": champion_id,
            "lane": "",
            "rank_segment": rank_segment,
            "win_rate": rate_to_percent(row.get("main_hero_win_rate")),
            "pick_rate": rate_to_percent(row.get("main_hero_appearance_rate")),
            "ban_rate": rate_to_percent(row.get("main_hero_ban_rate")),
            "match_type": str(row.get("match_type") or ""),
        })

        # Reference consumers interpret sub_hero as favorable for the main hero
        # and sub_hero_last as unfavorable. Keep the group in evidence_type so
        # this orientation remains auditable before the engine is switched.
        for group, sign in (("sub_hero", 1.0), ("sub_hero_last", -1.0)):
            for relation in row.get(group) or []:
                if not isinstance(relation, dict):
                    continue
                enemy_id = canonical_hero_id(relation.get("heroid"))
                if not enemy_id or enemy_id == champion_id:
                    continue
                try:
                    raw = abs(float(relation.get("increase_win_rate") or 0.0))
                except (TypeError, ValueError):
                    continue
                if raw <= 0:
                    continue
                key = (champion_id, enemy_id)
                candidate = MatchupEvidence(
                    champion_id=champion_id,
                    enemy_id=enemy_id,
                    raw_edge=sign * raw,
                    raw_unit="fraction",
                    source=MOONTON_GMS_SOURCE_NAME,
                    evidence_type=f"measured:{group}",
                    rank_segment=rank_segment,
                    sample_window="gms-current",
                    confidence=1.0,
                )
                old = evidence.get(key)
                if old is None or abs(candidate.raw_edge) > abs(old.raw_edge):
                    evidence[key] = candidate

    return {
        "heroes": list(heroes.values()),
        "stats": stats,
        "matchups": list(evidence.values()),
        "total": len(_records(payload)),
    }
