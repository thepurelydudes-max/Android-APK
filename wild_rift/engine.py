from __future__ import annotations

from collections import Counter, defaultdict
import re

import db
from draft_matrix_engine import DraftEdge, DraftMatrixEngine

ROLE_TO_LANES = {
    "Барон": {"top", "baron"},
    "Лес": {"jungle", "jg"},
    "Мид": {"mid"},
    "ADC": {"ad", "adc", "dragon", "duo", "bot"},
    "Саппорт": {"support", "sup"},
}
ROLE_TO_STAT = {"Барон": "top", "Лес": "jungle", "Мид": "mid", "ADC": "ad", "Саппорт": "support"}

# One shared matrix scorer is the only authority for the documented 60/20/15/5
# recommendation formula. Role filtering/data lookup stay in this module while
# the pure math lives in draft_matrix_engine.py and is regression-tested alone.
DRAFT_MATRIX = DraftMatrixEngine()
FINAL_WEIGHTS = dict(DRAFT_MATRIX.weights)

# WildRiftCore tiers are ordinal, so use equal steps rather than inventing
# nonlinear distances between neighbouring labels.  Unknown tier is treated as
# neutral (50) instead of punishing a champion for temporarily missing data.
TIER_SCORE = {"S+": 100.0, "S": 80.0, "A": 60.0, "B": 40.0, "C": 20.0, "D": 0.0}
TIER_ORDER = {"S+": 6, "S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}

# Canonical role order is also the deterministic tie-break order used by the
# automatic enemy-role inference.
CANONICAL_ROLES = ("Барон", "Лес", "Мид", "ADC", "Саппорт")

ITEM_TAGS = {
    "Mortal Reminder": {"anti_heal", "anti_armor", "anti_tank", "physical"},
    "Morellonomicon": {"anti_heal", "magic"},
    "Chempunk Chainsword": {"anti_heal", "physical", "fighter"},
    "Thornmail": {"anti_heal", "anti_physical", "defense", "tank"},
    "Randuin's Omen": {"anti_crit", "anti_physical", "defense", "tank"},
    "Frozen Heart": {"anti_attack_speed", "anti_physical", "defense", "tank"},
    "Plated Steelcaps": {"anti_physical", "anti_auto", "boots", "defense"},
    "Armored Advance": {"anti_physical", "anti_auto", "boots", "defense"},
    "Mercury's Treads": {"anti_magic", "anti_cc", "boots", "defense"},
    "Chainlaced Crushers": {"anti_magic", "anti_cc", "boots", "defense"},
    "Force of Nature": {"anti_magic", "defense", "tank"},
    "Spirit Visage": {"anti_magic", "defense", "tank"},
    "Kaenic Rookern": {"anti_magic", "defense", "tank"},
    "Maw of Malmortius": {"anti_magic", "anti_burst", "physical"},
    "Banshee's Veil": {"anti_magic", "anti_cc", "magic"},
    "Quicksilver Enchant": {"anti_cc", "boots"},
    "Mercurial Scimitar": {"anti_cc", "physical"},
    "Stasis Enchant": {"anti_burst", "boots", "universal"},
    "Zhonya's Hourglass": {"anti_burst", "magic"},
    "Guardian Angel": {"anti_burst", "physical", "defense"},
    "Sterak's Gage": {"anti_burst", "physical", "fighter", "defense"},
    "Edge of Night": {"anti_burst", "anti_cc", "physical"},
    "Serpent's Fang": {"anti_shield", "physical", "assassin"},
    "Oceanid's Trident": {"anti_shield", "magic"},
    "Blade of the Ruined King": {"anti_tank", "physical", "marksman", "fighter"},
    "Black Cleaver": {"anti_tank", "anti_armor", "physical", "fighter"},
    "Serylda's Grudge": {"anti_armor", "physical"},
    "Dominik's Regards": {"anti_armor", "anti_tank", "physical", "marksman"},
    "Terminus": {"anti_armor", "anti_tank", "physical", "marksman"},
    "Liandry's Torment": {"anti_tank", "magic"},
    "Void Staff": {"anti_magic_resist", "magic"},
    "Cryptbloom": {"anti_magic_resist", "magic"},
    "Divine Sunderer": {"anti_tank", "physical", "fighter"},
    "Dead Man's Plate": {"anti_physical", "defense", "tank"},
    "Abyssal Mask": {"anti_magic", "defense", "tank"},
    "Mantle of the Twelfth Hour": {"anti_burst", "defense", "tank"},
    "Iceborn Gauntlet": {"anti_physical", "defense", "tank"},
    "Sunfire Aegis": {"anti_physical", "defense", "tank"},
    "Warmog's Armor": {"defense", "tank"},
    "Amaranth's Twinguard": {"anti_burst", "defense", "tank"},
    "Unending Despair": {"defense", "tank"},
    "Heartsteel": {"defense", "tank"},
    "Radiant Virtue": {"support", "defense", "tank"},
    "Knight's Vow": {"support", "defense"},
    "Redemption": {"support", "heal"},
    "Harmonic Echo": {"support", "heal"},
    "Ardent Censer": {"support"},
    "Staff of Flowing Water": {"support"},
    "Shurelya's Battlesong": {"support"},
    "Mikael's Blessing": {"support", "anti_cc"},
    "Imperial Mandate": {"support"},
}

# Source names vary slightly by apostrophe/case.
def norm_item(s: str) -> str:
    s = s.replace("’", "'").strip().casefold()
    return re.sub(r"\s+", " ", s)


def is_boot_item(name: str, category: str = "") -> bool:
    text = norm_item(name)
    cat = str(category or "").casefold()
    # Wild Rift current boots use both explicit category=Boots and names such
    # as Armored Advance / Chainlaced Crushers, so category is authoritative.
    return (
        "boot" in text or "boots" in cat or "boot" in cat
        or "tread" in text or "greaves" in text
        or "advance" in text or "crushers" in text
        or text in {"armorcrusher boots", "crimson lucidity", "immortal boots", "spellslinger's shoes"}
    )


_ITEM_TAGS_BY_NORM = {norm_item(name): frozenset(tags) for name, tags in ITEM_TAGS.items()}


def tags_for(item: str) -> set[str]:
    return set(_ITEM_TAGS_BY_NORM.get(norm_item(item), ()))


def lane_ok(champ: dict, role_ru: str) -> bool:
    allowed = ROLE_TO_LANES.get(role_ru, set())
    lanes = {str(x).casefold() for x in champ.get("lanes", [])}
    return bool(lanes & allowed)


def _role_pick_rate(champ: dict, role_ru: str, snapshot: dict | None = None) -> float | None:
    """Return current all-rank pick rate for an exact champion+role, if known."""
    stat_lane = ROLE_TO_STAT.get(role_ru, "")
    if not stat_lane:
        return None
    st = _stat(champ.get("id", ""), stat_lane, "all", snapshot)
    if not st or st.get("pick_rate") is None:
        return None
    try:
        return max(0.0, float(st.get("pick_rate") or 0.0))
    except (TypeError, ValueError):
        return None


def _primary_recommendation_role(
    champ: dict,
    snapshot: dict | None = None,
) -> str:
    """Assign every champion to exactly one recommendation branch.

    Flex-lane metadata is still used for enemy-role inference and exact build
    lookup, but a champion must not appear in several recommendation branches
    at once. Among roles that have a complete WildRiftCore build, prefer the
    role where the champion is actually played most according to current
    all-rank pick rate. If pick-rate data is unavailable/tied, use current role
    tier and finally the upstream lane order as deterministic fallbacks.
    """
    lanes = [str(x).casefold() for x in champ.get("lanes", [])]
    lane_position: dict[str, int] = {}
    for index, lane in enumerate(lanes):
        for role, aliases in ROLE_TO_LANES.items():
            if lane in aliases and role not in lane_position:
                lane_position[role] = index

    candidates: list[tuple[int, float, int, int, int, str]] = []
    for canonical_index, role in enumerate(CANONICAL_ROLES):
        if not lane_ok(champ, role):
            continue
        if not _has_usable_role_build(champ, role, snapshot):
            continue

        pick_rate = _role_pick_rate(champ, role, snapshot)
        has_pick_rate = 1 if pick_rate is not None else 0
        role_tier = TIER_ORDER.get(_tier(champ.get("id", ""), role, snapshot), 0)
        lane_rank = -lane_position.get(role, 999)
        candidates.append((
            has_pick_rate,
            float(pick_rate or 0.0),
            role_tier,
            lane_rank,
            -canonical_index,
            role,
        ))

    return max(candidates)[-1] if candidates else ""


def archetype(champ: dict) -> set[str]:
    roles = {str(x).casefold() for x in champ.get("roles", [])}
    typ = str(champ.get("damage_type", "")).casefold()
    out = set(roles)
    if "ap" in typ or "magic" in typ:
        out.add("magic")
    if "ad" in typ or "physical" in typ:
        out.add("physical")
    return out


def _find_champ(value: str, snapshot: dict | None = None):
    if snapshot is not None:
        return db.resolve_snapshot_champion(snapshot, value)
    return db.champion_by_name_or_id(value)


def _snapshot_matchup_score(snapshot: dict, champion_id: str, enemy_id: str, preferred_role: str = "") -> float:
    rows = snapshot.get("matchups", {}).get((champion_id, enemy_id), [])
    if not rows:
        return 0.0
    role_norm = preferred_role.casefold()
    if role_norm:
        exact = [float(score) for role, score in rows if str(role).casefold() == role_norm]
        if exact:
            return max(exact, key=abs)
        # A role-specific source must never leak a Jungle verdict into Top, etc.
        # Only an explicit general-role row may act as fallback.
        general = [float(score) for role, score in rows if not str(role).strip()]
        return max(general, key=abs) if general else 0.0
    return max((float(score) for _role, score in rows), key=abs)

def _matchup_score(champion_id: str, enemy_id: str, preferred_role: str = "", snapshot: dict | None = None) -> float:
    if snapshot is not None:
        return _snapshot_matchup_score(snapshot, champion_id, enemy_id, preferred_role)
    return db.get_matchup_score(champion_id, enemy_id, preferred_role)


def _stat(champion_id: str, lane: str, rank_segment: str = "all", snapshot: dict | None = None):
    if snapshot is not None:
        return snapshot.get("stats", {}).get((champion_id, lane, rank_segment))
    return db.get_stat(champion_id, lane, rank_segment)


def _tier(champion_id: str, role_ru: str, snapshot: dict | None = None) -> str:
    if snapshot is not None:
        return str(snapshot.get("tiers", {}).get((champion_id, role_ru), "") or "").upper()
    return str(db.get_champion_tier(champion_id, role_ru) or "").upper()


def _role_evidence(champ: dict, role_ru: str, snapshot: dict | None = None) -> float | None:
    """Return evidence that *champ* is actually played in ``role_ru``.

    The database already contains role information in three independent places:
    champion lanes, role-specific statistics and role-specific tier rows.  Pick
    rate is the strongest role-priority signal when available; lane/tier presence
    keeps the inference working when statistics are temporarily missing.
    """
    allowed = ROLE_TO_LANES.get(role_ru, set())
    lanes = {str(x).casefold() for x in champ.get("lanes", [])}
    lane_known = bool(lanes & allowed)

    # Champion lane metadata is the legality gate for draft recommendations.
    # Stats/tier sources are ranking evidence only. They may contain niche/off-meta
    # samples (for example Malphite Mid at ~1% pick rate) or parser noise and must
    # never invent a new selectable role for a champion whose lane list is known.
    if lanes and not lane_known:
        return None

    stat_lane = ROLE_TO_STAT.get(role_ru, "")
    st = _stat(champ.get("id", ""), stat_lane, "all", snapshot) if stat_lane else None
    tier = _tier(champ.get("id", ""), role_ru, snapshot)

    if not lane_known and not st and not tier:
        return None

    pick_rate = 0.0
    if st and st.get("pick_rate") is not None:
        try:
            pick_rate = max(0.0, float(st.get("pick_rate") or 0.0))
        except (TypeError, ValueError):
            pick_rate = 0.0

    # Pick rate naturally separates a champion's primary and secondary roles.
    # The small fixed terms are only presence evidence, not recommendation points.
    return pick_rate + (2.0 if lane_known else 0.0) + (1.0 if tier else 0.0)


_ENEMY_ROLE_CACHE: dict[tuple, tuple[tuple[str, str], ...]] = {}


def _infer_enemy_roles(enemy_objs: list[tuple[dict, str]], snapshot: dict | None = None) -> list[tuple[dict, str]]:
    """Infer the most likely enemy positions from data already stored in the DB.

    Role inference used to be repeated once for recommend_picks() and then again
    for every one of the ten build previews. On mobile that means evaluating up
    to 120 role permutations eleven times for the exact same enemy draft.
    Cache only the resulting champion-id/role pairs for the lifetime of the
    immutable runtime snapshot.
    """
    from itertools import permutations

    cache_key = None
    if snapshot is not None:
        cache_key = (
            id(snapshot),
            tuple((str(champ.get("id") or ""), str(role or "")) for champ, role in enemy_objs),
        )
        cached = _ENEMY_ROLE_CACHE.get(cache_key)
        if cached is not None:
            by_id = snapshot.get("champions_by_id", {})
            restored = [
                (by_id.get(champion_id), role)
                for champion_id, role in cached
            ]
            if all(champ is not None for champ, _role in restored):
                return [(champ, role) for champ, role in restored if champ is not None]

    result: list[list] = [[champ, str(role or "")] for champ, role in enemy_objs]
    fixed_roles = {role for _champ, role in result if role in CANONICAL_ROLES}
    unresolved = [i for i, (_champ, role) in enumerate(result) if role not in CANONICAL_ROLES]
    available = [r for r in CANONICAL_ROLES if r not in fixed_roles]

    best_perm = None
    best_score = None
    if unresolved and len(unresolved) <= len(available):
        for perm in permutations(available, len(unresolved)):
            total = 0.0
            valid = True
            for idx, role in zip(unresolved, perm):
                evidence = _role_evidence(result[idx][0], role, snapshot)
                if evidence is None:
                    valid = False
                    break
                total += evidence
            if valid and (best_score is None or total > best_score):
                best_score = total
                best_perm = perm

    assigned = set()
    if best_perm is not None:
        for idx, role in zip(unresolved, best_perm):
            result[idx][1] = role
            assigned.add(idx)

    # Off-meta or incomplete drafts may not admit a unique five-role assignment.
    # Fall back to each champion's best known role without forcing fake data.
    for idx in unresolved:
        if idx in assigned:
            continue
        options = []
        for order, role in enumerate(CANONICAL_ROLES):
            evidence = _role_evidence(result[idx][0], role, snapshot)
            if evidence is not None:
                options.append((evidence, -order, role))
        if options:
            result[idx][1] = max(options)[2]

    final = [(champ, role) for champ, role in result]
    if cache_key is not None:
        if len(_ENEMY_ROLE_CACHE) >= 128:
            _ENEMY_ROLE_CACHE.clear()
        _ENEMY_ROLE_CACHE[cache_key] = tuple(
            (str(champ.get("id") or ""), role) for champ, role in final
        )
    return final


def _winrate_percentile(win_rate: float | None, population: list[float]) -> float:
    """Map role win rate to a 0..100 percentile without arbitrary WR point bonuses."""
    if win_rate is None or not population:
        return 50.0
    try:
        wr = float(win_rate)
    except (TypeError, ValueError):
        return 50.0
    lower = sum(1 for value in population if value < wr)
    equal = sum(1 for value in population if value == wr)
    return 100.0 * (lower + 0.5 * equal) / len(population)


def _finished_item(item_name: str, snapshot: dict | None = None) -> bool:
    """Return True only for catalog entries explicitly marked as final Upgraded items."""
    row = None
    if snapshot is not None:
        items = snapshot.get("items", {})
        row = items.get(item_name)
        if row is None:
            index = snapshot.get("_items_by_engine_norm")
            if index is None:
                index = {norm_item(name): value for name, value in items.items()}
                snapshot["_items_by_engine_norm"] = index
            row = index.get(norm_item(item_name))
    else:
        row = db.get_item(item_name)
    return bool(row and str(row.get("tier") or "").strip().casefold() == "upgraded")


def _item_pool(champion_id: str, snapshot: dict | None = None) -> list[dict]:
    rows = (
        list(snapshot.get("item_pools", {}).get(champion_id, []))
        if snapshot is not None else db.get_item_pool(champion_id)
    )
    return [row for row in rows if _finished_item(row.get("item_name", ""), snapshot)]


def _counter_items(enemy_id: str, snapshot: dict | None = None) -> list[dict]:
    rows = (
        list(snapshot.get("counter_items", {}).get(enemy_id, []))
        if snapshot is not None else db.get_counter_items(enemy_id)
    )
    return [row for row in rows if _finished_item(row.get("item_name", ""), snapshot)]


def _has_usable_role_build(
    champ: dict,
    role_ru: str,
    snapshot: dict | None = None,
) -> bool:
    """Return True only for a healthy exact champion+role WRC build.

    Lane metadata is not sufficient for recommendations. The exact role must
    have five finished WildRiftCore core items plus a valid finished boot; if
    not, the champion is not recommendable on that role.
    """
    row = _role_build_row(champ.get("id", ""), role_ru, snapshot)
    if row is None:
        return False
    core = _finished_only(
        [str(x) for x in (row.get("items") or [])],
        snapshot,
    )
    boot = str(row.get("boot_name") or "")
    return (
        len(core) == 5
        and bool(boot)
        and _finished_item(boot, snapshot)
    )


_PICK_GRID_CACHE: dict[tuple, dict[str, list[dict]]] = {}


def _role_draft_rank_key(row: dict) -> tuple:
    """Local role ranking for one exact champion+role pair.

    The draft matrix sum is the primary authority. Coverage only breaks equal
    sums, so +1 +1 ranks above +2 +0 while +3 +0 still beats +1 +1.
    The user-facing Draft Score follows matchup_sum and coverage_count so it
    cannot visually contradict the list. The legacy 60/20/15/5 meta score,
    tier and win-rate are retained only as later deterministic tie-breakers.
    """
    return (
        float(row.get("matchup_sum") or 0.0),
        int(row.get("coverage_count") or 0),
        float(row.get("raw_positive_strength") or 0.0),
        -int(row.get("negative_count") or 0),
        -float(row.get("raw_negative_strength") or 0.0),
        float(row.get("meta_score") or 0.0),
        float(row.get("matchup_score") or 0.0),
        TIER_ORDER.get(str(row.get("tier") or ""), 0),
        float(row.get("winrate_score") or 0.0),
        not bool(row.get("lane_hard_loss", False)),
    )


def _rank_role_candidates(
    role_ru: str,
    enemy_objs: list[tuple[dict, str]],
    enemy_ids: set[str],
    snapshot: dict | None = None,
) -> list[dict]:
    """Build the complete role-specific candidate ranking before global allocation."""
    stat_lane = ROLE_TO_STAT.get(role_ru, "")

    candidate_rows: list[tuple[dict, str, dict | None]] = []
    for cand in (snapshot.get("champions", []) if snapshot is not None else db.champions()):
        if cand.get("id") in enemy_ids:
            continue
        # Flex champions may legitimately participate in several *candidate*
        # branches. Global allocation below decides the one branch where the
        # champion will finally be shown.
        if not lane_ok(cand, role_ru):
            continue
        if not _has_usable_role_build(cand, role_ru, snapshot):
            continue
        tier = _tier(cand.get("id", ""), role_ru, snapshot)
        st = _stat(cand.get("id", ""), stat_lane, "all", snapshot) if stat_lane else None
        candidate_rows.append((cand, tier, st))

    role_win_rates: list[float] = []
    for _cand, _tier_value, st in candidate_rows:
        if st and st.get("win_rate") is not None:
            try:
                role_win_rates.append(float(st["win_rate"]))
            except (TypeError, ValueError):
                pass

    out: list[dict] = []
    for cand, tier, st in candidate_rows:
        matrix_edges: list[DraftEdge] = []
        mirror_edge: float | None = None
        raw_edges: list[float] = []

        for enemy, inferred_role in enemy_objs:
            edge = float(_matchup_score(cand["id"], enemy["id"], role_ru, snapshot))
            edge = DRAFT_MATRIX.clamp_edge(edge)
            raw_edges.append(edge)
            weight = DRAFT_MATRIX.lane_weight(role_ru, inferred_role)
            if inferred_role == role_ru:
                mirror_edge = edge
            matrix_edges.append(DraftEdge(
                enemy_id=str(enemy.get("id") or ""),
                enemy_name=str(enemy.get("name") or enemy.get("id") or ""),
                enemy_role=str(inferred_role or ""),
                edge=edge,
                weight=weight,
            ))

        matrix = DRAFT_MATRIX.analyze_row(matrix_edges)
        matrix["mirror_edge"] = mirror_edge

        tier_score = TIER_SCORE.get(tier, 50.0)

        wr = None
        if st and st.get("win_rate") is not None:
            try:
                wr = float(st["win_rate"])
            except (TypeError, ValueError):
                wr = None
        winrate_score = _winrate_percentile(wr, role_win_rates)

        final = DRAFT_MATRIX.final_score(
            matchup_score=matrix["matchup_score"],
            coverage_score=matrix["coverage_score"],
            tier_score=tier_score,
            winrate_score=winrate_score,
        )

        raw_matchup_sum = sum(raw_edges)
        draft_score = DRAFT_MATRIX.draft_score(
            matchup_sum=raw_matchup_sum,
            coverage_count=matrix["coverage_count"],
        )

        direct_lane_edges = matrix["direct_lane_edges"]
        lane_hard_loss = any(edge <= -2.0 for edge in direct_lane_edges)

        raw_positive_strength = sum(edge for edge in raw_edges if edge > 0)
        raw_negative_strength = sum(abs(edge) for edge in raw_edges if edge < 0)
        negative_count = sum(1 for edge in raw_edges if edge < 0)

        out.append({
            "champion": cand,
            "role": role_ru,
            "score": draft_score,
            "meta_score": final["score"],
            "positive": matrix["positive"],
            "negative": matrix["negative"],
            "neutral": matrix["neutral"],
            "positive_strength": matrix["positive_strength"],
            "negative_strength": matrix["negative_strength"],
            "raw_positive_strength": raw_positive_strength,
            "raw_negative_strength": raw_negative_strength,
            "negative_count": negative_count,
            "matchup_sum": raw_matchup_sum,
            "hard_counters": matrix["hard_counters"],
            "coverage_count": matrix["coverage_count"],
            "coverage_total": matrix["coverage_total"],
            "coverage_score": matrix["coverage_score"],
            "coverage_bonus": final["coverage"],
            "coverage_weight": matrix["positive_weight"],
            "draft_weight": matrix["total_weight"],
            "tier": tier,
            "tier_score": tier_score,
            "tier_bonus": final["tier"],
            "win_rate": wr,
            "winrate_score": winrate_score,
            "matchup_score": matrix["matchup_score"],
            "matchup_contribution": final["matchup"],
            "winrate_bonus": final["winrate"],
            "lane_hard_loss": lane_hard_loss,
            "mirror_edge": mirror_edge,
            "mirror_bucket": 2 if (mirror_edge is not None and mirror_edge > 0) else (1 if mirror_edge is None or mirror_edge == 0 else 0),
            "enemy_roles": matrix["enemy_roles"],
            "matrix_row": matrix["matrix_row"],
        })

    out.sort(key=_role_draft_rank_key, reverse=True)
    for index, row in enumerate(out, start=1):
        row["local_rank"] = index
    return out


def _next_available_row(
    rows: list[dict],
    assigned: set[str],
) -> dict | None:
    for row in rows:
        cid = str((row.get("champion") or {}).get("id") or "")
        if cid and cid not in assigned:
            return row
    return None


def _marginal_role_gap(
    role: str,
    candidate: dict,
    ranked: dict[str, list[dict]],
    assigned: set[str],
) -> tuple:
    """How much this role deteriorates if *candidate* is taken elsewhere."""
    cid = str((candidate.get("champion") or {}).get("id") or "")
    next_row = None
    seen_candidate = False
    for row in ranked.get(role, []):
        row_cid = str((row.get("champion") or {}).get("id") or "")
        if not row_cid or row_cid in assigned:
            continue
        if row_cid == cid and not seen_candidate:
            seen_candidate = True
            continue
        next_row = row
        break

    if next_row is None:
        return (float("inf"), float("inf"), float("inf"), float("inf"))

    return (
        float(candidate.get("matchup_sum") or 0.0) - float(next_row.get("matchup_sum") or 0.0),
        int(candidate.get("coverage_count") or 0) - int(next_row.get("coverage_count") or 0),
        float(candidate.get("raw_positive_strength") or 0.0) - float(next_row.get("raw_positive_strength") or 0.0),
        float(candidate.get("meta_score") or 0.0) - float(next_row.get("meta_score") or 0.0),
    )


def _conflict_role_key(
    role: str,
    candidate: dict,
    ranked: dict[str, list[dict]],
    assigned: set[str],
    snapshot: dict | None,
) -> tuple:
    """Resolve one flex champion proposed by several branches.

    Prefer the branch where the champion is locally higher. If the local place
    is tied, prefer the stronger role-specific matchup sum. If that is tied too,
    keep the champion where removing it would hurt the branch most. Remaining
    ties use coverage, current score and finally the champion's statistical
    primary role only as deterministic fallbacks.
    """
    champ = candidate.get("champion") or {}
    primary_role = _primary_recommendation_role(champ, snapshot)
    return (
        -int(candidate.get("local_rank") or 10_000),
        float(candidate.get("matchup_sum") or 0.0),
        _marginal_role_gap(role, candidate, ranked, assigned),
        int(candidate.get("coverage_count") or 0),
        float(candidate.get("raw_positive_strength") or 0.0),
        -float(candidate.get("raw_negative_strength") or 0.0),
        float(candidate.get("meta_score") or 0.0),
        1 if primary_role == role else 0,
        -CANONICAL_ROLES.index(role),
    )


def _allocate_unique_role_grid(
    ranked: dict[str, list[dict]],
    limit: int,
    snapshot: dict | None = None,
) -> dict[str, list[dict]]:
    """Distribute flex champions across role lists without duplicates.

    Allocation happens one visible rank at a time. A champion can compete in
    every exact WRC role it genuinely supports, but once assigned to one branch
    it is removed globally from all other branches.
    """
    result = {role: [] for role in CANONICAL_ROLES}
    assigned: set[str] = set()
    wanted = max(0, int(limit))

    for _slot in range(wanted):
        unresolved = {
            role for role in CANONICAL_ROLES
            if len(result[role]) < wanted
            and _next_available_row(ranked.get(role, []), assigned) is not None
        }
        if not unresolved:
            break

        while unresolved:
            proposals: dict[str, dict] = {}
            for role in tuple(unresolved):
                row = _next_available_row(ranked.get(role, []), assigned)
                if row is None:
                    unresolved.discard(role)
                else:
                    proposals[role] = row

            if not proposals:
                break

            by_champion: dict[str, list[str]] = {}
            for role, row in proposals.items():
                cid = str((row.get("champion") or {}).get("id") or "")
                if cid:
                    by_champion.setdefault(cid, []).append(role)

            progress = False
            for cid, roles in by_champion.items():
                if len(roles) == 1:
                    winner = roles[0]
                else:
                    winner = max(
                        roles,
                        key=lambda role: _conflict_role_key(
                            role, proposals[role], ranked, assigned, snapshot
                        ),
                    )

                chosen = proposals[winner]
                result[winner].append(chosen)
                assigned.add(cid)
                unresolved.discard(winner)
                progress = True
                # Losing roles stay unresolved and will immediately propose
                # their next available champion now that this cid is globally
                # unavailable.

            if not progress:
                break

    return result


def recommend_pick_grid(
    enemies: list[tuple[str, str]],
    limit: int = 8,
    snapshot: dict | None = None,
) -> dict[str, list[dict]]:
    """Return all five role lists from one globally unique draft allocation."""
    raw_enemy_objs: list[tuple[dict, str]] = []
    for name, enemy_role in enemies:
        champ = _find_champ(name, snapshot)
        if champ:
            raw_enemy_objs.append((champ, enemy_role))
    if not raw_enemy_objs:
        return {role: [] for role in CANONICAL_ROLES}

    cache_key = None
    if snapshot is not None:
        cache_key = (
            id(snapshot),
            tuple((str(champ.get("id") or ""), str(role or "")) for champ, role in raw_enemy_objs),
            int(limit),
        )
        cached = _PICK_GRID_CACHE.get(cache_key)
        if cached is not None:
            return cached

    enemy_objs = _infer_enemy_roles(raw_enemy_objs, snapshot)
    enemy_ids = {enemy["id"] for enemy, _enemy_role in enemy_objs if enemy.get("id")}

    ranked = {
        role: _rank_role_candidates(role, enemy_objs, enemy_ids, snapshot)
        for role in CANONICAL_ROLES
    }
    grid = _allocate_unique_role_grid(ranked, limit, snapshot)

    if cache_key is not None:
        if len(_PICK_GRID_CACHE) >= 64:
            _PICK_GRID_CACHE.clear()
        _PICK_GRID_CACHE[cache_key] = grid
    return grid


def recommend_picks(role_ru: str, enemies: list[tuple[str, str]], limit: int = 8, snapshot: dict | None = None) -> list[dict]:
    """Return one branch from the globally unique five-role recommendation grid."""
    if role_ru not in CANONICAL_ROLES:
        return []
    return list(recommend_pick_grid(enemies, limit=limit, snapshot=snapshot).get(role_ru, []))

BUILD_NEED_TAGS = {
    "anti_heal", "anti_shield", "anti_crit", "anti_attack_speed",
    "anti_physical", "anti_magic", "anti_cc", "anti_burst",
    "anti_tank", "anti_armor", "anti_magic_resist", "anti_auto",
    "anti_mobility", "anti_dive", "anti_duelist", "anti_poke",
    "anti_engage",
}
CORE_SITUATIONAL_TAGS = {
    "anti_heal", "anti_shield", "anti_crit", "anti_attack_speed",
    "anti_magic", "anti_physical", "anti_cc", "anti_burst",
    # Penetration / anti-tank items are answers to a specific enemy build, not
    # generic core purchases. Keeping them out of the first-pass core prevents
    # things such as Void Staff from appearing just because it is present in a
    # broad Build Trends pool.
    "anti_tank", "anti_armor", "anti_magic_resist",
}


def _item_record(item_name: str, snapshot: dict | None = None) -> dict | None:
    if snapshot is not None:
        items = snapshot.get("items", {})
        row = items.get(item_name)
        if row is not None:
            return row
        wanted = norm_item(item_name)
        return next((value for key, value in items.items() if norm_item(key) == wanted), None)
    return db.get_item(item_name)


def _item_category(item_name: str, snapshot: dict | None = None) -> str:
    row = _item_record(item_name, snapshot) or {}
    return str(row.get("category") or "").strip()


def _semantic_pool_category(row: dict, snapshot: dict | None = None) -> str:
    """Normalize WR Pocket's item groups into build roles.

    WR Pocket is an excellent *pool* source, but its page groups are not a
    six-item purchase order. Some utility items (for example Redemption) can
    also be listed under Magic. Semantic tags keep those items in the support
    branch without modifying the source database.
    """
    name = str(row.get("item_name") or row.get("name") or "")
    tags = tags_for(name)
    if "boots" in tags or is_boot_item(name, row.get("category") or ""):
        return "Boots"
    if "support" in tags:
        return "Support"
    category = str(row.get("category") or "").strip()
    if not category:
        category = _item_category(name, snapshot)
    return category or "Other"


def _pool_category_counts(pool: list[dict], snapshot: dict | None = None) -> Counter:
    return Counter(_semantic_pool_category(row, snapshot) for row in pool)


def _direct_need_tags(direct_items: list[dict]) -> set[str]:
    """Extract the shared mechanic behind a generic enemy counter-item list."""
    if not direct_items:
        return set()

    counts: Counter = Counter()
    for row in direct_items:
        for tag in tags_for(str(row.get("item_name") or "")) & BUILD_NEED_TAGS:
            counts[tag] += 1

    threshold = 1 if len(direct_items) == 1 else max(2, (len(direct_items) + 3) // 4)
    return {tag for tag, amount in counts.items() if amount >= threshold}


def _generic_threat_tags(enemy: dict, snapshot: dict | None = None) -> set[str]:
    """Low-weight fallback based on the enemy's own current build pool.

    champions.damage_type is intentionally ignored: the upstream field currently
    contains resource types such as Mana/Energy rather than reliable damage type.
    """
    roles = {str(x).casefold() for x in enemy.get("roles", [])}
    pool = _item_pool(str(enemy.get("id") or ""), snapshot)
    counts = _pool_category_counts(pool, snapshot)
    physical = int(counts.get("Physical", 0))
    magic = int(counts.get("Magic", 0))
    tags: set[str] = set()

    if "marksman" in roles and "mage" in roles:
        tags |= {"anti_physical", "anti_magic"}
    elif physical and not magic:
        tags.add("anti_physical")
    elif magic and not physical:
        tags.add("anti_magic")
    elif physical and magic:
        if physical >= magic * 2:
            tags.add("anti_physical")
        elif magic >= physical * 2:
            tags.add("anti_magic")
        else:
            tags |= {"anti_physical", "anti_magic"}

    if "marksman" in roles:
        tags |= {"anti_crit", "anti_auto"}
    if "assassin" in roles:
        tags |= {"anti_burst", "anti_dive"}
    # Fighter is deliberately NOT synonymous with duelist. WRC's counter
    # labels / example drafts provide that semantic signal directly; treating
    # every fighter as a duelist made Sustain variants over-trigger.
    if "tank" in roles:
        tags.add("anti_tank")
    return tags


def _default_build_role(champ: dict) -> str:
    for role in CANONICAL_ROLES:
        if lane_ok(champ, role):
            return role
    return ""


def _finished_only(names: list[str], snapshot: dict | None = None) -> list[str]:
    """Hard final-build gate: recipe components can never reach the UI.

    item_pools and counter_items are already filtered to WR Pocket tier=Upgraded,
    but keep this independent last line of defense in the engine so a future
    parser/source regression cannot surface Basic, Mid-tier or Starter items as
    a finished six-slot recommendation.
    """
    out: list[str] = []
    for name in names or []:
        if name in out:
            continue
        if _finished_item(name, snapshot):
            out.append(name)
    return out


def _role_build_row(champion_id: str, role_ru: str, snapshot: dict | None = None) -> dict | None:
    if snapshot is not None:
        return snapshot.get("role_builds", {}).get((champion_id, role_ru))
    return db.get_role_build(champion_id, role_ru)


def _role_variant_rows(champion_id: str, role_ru: str, snapshot: dict | None = None) -> list[dict]:
    if snapshot is not None:
        return list(snapshot.get("role_variants", {}).get((champion_id, role_ru), []))
    return db.get_role_build_variants(champion_id, role_ru)


def _variant_tags(name: str, trigger: str) -> set[str]:
    """Extract enemy conditions, never our build style, from a WRC variant.

    Titles such as "Standard — AP burst" or "Sustain — brawler" describe what
    *our champion* is building, not what the enemy team is doing. The source's
    "When to pick it" / example explanation is authoritative for enemy signals.
    Only explicit "Vs AD/AP comps" wording in the title is itself a condition.
    """
    trigger_text = str(trigger or "")
    tags = _trigger_tags_from_text(trigger_text)
    title = str(name or "").casefold()
    if any(token in title for token in ("vs ad", "vs physical")):
        tags.add("anti_physical")
    if any(token in title for token in ("vs ap", "vs magic")):
        tags.add("anti_magic")
    return tags


def _variant_example_matches(
    row: dict,
    enemy_objs: list[tuple[dict, str]] | None,
) -> int:
    if not enemy_objs:
        return 0
    wanted = {
        norm_item(str(value))
        for value in (row.get("example_enemies") or [])
        if str(value).strip()
    }
    if not wanted:
        return 0
    draft = set()
    for enemy, _role in enemy_objs:
        draft.add(norm_item(str(enemy.get("id") or "")))
        draft.add(norm_item(str(enemy.get("name") or "")))
    return len({value for value in wanted if value and value in draft})


def _wrc_example_threat_tags(
    enemy: dict,
    snapshot: dict | None = None,
) -> set[str]:
    """Learn enemy archetype tags from WRC's own example-draft explanations.

    Example drafts contain neutral fillers too, so only tag an example champion
    when WRC explicitly names that champion in the accompanying explanation
    (e.g. "Dive threat: Fiora, Lee Sin" or "Extended duels against Jax, Xin Zhao").
    """
    if snapshot is None:
        return set()
    enemy_keys = {
        norm_item(str(enemy.get("id") or "")),
        norm_item(str(enemy.get("name") or "")),
    }
    enemy_keys.discard("")
    if not enemy_keys:
        return set()

    tags: set[str] = set()
    for rows in (snapshot.get("role_variants", {}) or {}).values():
        for row in rows:
            trigger_text = str(row.get("trigger_text") or "")
            # These examples describe our allied composition, not an enemy
            # threat archetype. Store them for future ally-aware drafting but
            # never learn enemy labels from them.
            if "allied" in trigger_text.casefold():
                continue
            example_text = str(row.get("example_text") or "")
            if not example_text:
                continue
            folded = norm_item(example_text)
            if not any(key and key in folded for key in enemy_keys):
                continue
            tags |= _variant_tags(
                str(row.get("variant_name") or ""),
                " ".join([
                    str(row.get("trigger_text") or ""),
                    example_text,
                ]),
            )
    return tags


def _wrc_counter_trait_tags(
    enemy: dict,
    snapshot: dict | None = None,
) -> set[str]:
    """Map patch-native WRC Counter labels onto build-condition tags."""
    if snapshot is None:
        return set()
    rows = list((snapshot.get("champion_traits") or {}).get(
        str(enemy.get("id") or ""), []
    ))
    mapping = {
        "tank": "anti_tank",
        "duelist": "anti_duelist",
        "dive": "anti_dive",
        "burst": "anti_burst",
        "poke": "anti_poke",
        "engage": "anti_engage",
        "cc": "anti_cc",
        "healing": "anti_heal",
        "shield": "anti_shield",
        "mobility": "anti_mobility",
        "physical": "anti_physical",
        "magic": "anti_magic",
    }
    out: set[str] = set()
    for row in rows:
        try:
            confidence = float(row.get("confidence") or 0.0)
            evidence = int(row.get("evidence_count") or 0)
        except (TypeError, ValueError):
            continue
        # A repeated WRC label is enough even when a champion appears in many
        # matchup pages; a single observation is accepted only when it is a
        # large share of that champion's source descriptions.
        if not (
            (evidence >= 2 and confidence >= 0.08)
            or confidence >= 0.30
        ):
            continue
        mapped = mapping.get(str(row.get("trait") or ""))
        if mapped:
            out.add(mapped)
    return out


def _wrc_opponent_reason_threat_tags(
    enemy: dict,
    snapshot: dict | None = None,
) -> set[str]:
    """Learn threat semantics from WRC Adaptations-by-opponent globally."""
    if snapshot is None:
        return set()
    wanted = {
        norm_item(str(enemy.get("id") or "")),
        norm_item(str(enemy.get("name") or "")),
        norm_item(str(enemy.get("name_ru") or "")),
    }
    wanted.discard("")
    tags: set[str] = set()
    for rows in (snapshot.get("role_opponent_adaptations", {}) or {}).values():
        for row in rows:
            enemy_name = norm_item(str(
                row.get("enemy_name") or row.get("enemy_name_norm") or ""
            ))
            if enemy_name and enemy_name in wanted:
                tags |= _trigger_tags_from_text(str(row.get("reason") or ""))
    return tags


def _select_role_variant(
    rows: list[dict], threat_counts: Counter,
    enemy_objs: list[tuple[dict, str]] | None = None,
) -> dict | None:
    """Choose one complete WRC variant from the enemy draft.

    Standard is the safe fallback. Resistance variants require a real team-level
    signal (normally 3+ enemies), preventing one physical/magic champion from
    replacing the entire five-item source build.
    """
    if not rows:
        return None
    standard = next(
        (row for row in rows if str(row.get("variant_name") or "").casefold().startswith("standard")),
        rows[0],
    )
    best: tuple[float, int, dict] | None = None
    physical = int(threat_counts.get("anti_physical", 0))
    magic = int(threat_counts.get("anti_magic", 0))

    for row in rows:
        name = str(row.get("variant_name") or "")
        trigger = str(row.get("trigger_text") or "")
        example_text = str(row.get("example_text") or "")
        trigger_folded = trigger.casefold()
        # Pure ally-dependent variants cannot be inferred from WRCA's current
        # enemy-only input. Mixed rules still use their observable enemy clause,
        # e.g. "2+ dive threats 2+ allied carries to protect".
        pure_allied_rule = (
            "allied engages" in trigger_folded
            or (
                "allied carries" in trigger_folded
                and "dive threat" not in trigger_folded
            )
            or (
                "allied" in trigger_folded
                and not any(token in trigger_folded for token in (
                    "dive threat", "poke champion",
                    "hard engage", "crowd-control", "crowd control",
                    "mostly physical", "mostly magic",
                ))
            )
        )
        if pure_allied_rule:
            continue
        tags = _variant_tags(name, f"{trigger} {example_text}")
        if not tags:
            continue

        example_matches = _variant_example_matches(row, enemy_objs)
        score = 0.0
        if "anti_physical" in tags:
            # WRC examples for "Mostly physical damage" commonly identify three
            # dominant physical threats. Require a majority, not an arbitrary
            # 4/5 composition.
            if physical < 3 or physical <= magic:
                continue
            score += 10.0 + physical * 2.0 - magic
        if "anti_magic" in tags:
            if magic < 3 or magic <= physical:
                continue
            score += 10.0 + magic * 2.0 - physical

        other = tags - {"anti_physical", "anti_magic"}
        if other:
            counts = [int(threat_counts.get(tag, 0)) for tag in other]
            thresholds = [
                int(value)
                for value in re.findall(r"(\d+)\s*\+", trigger_folded)
            ]
            threshold = max(thresholds, default=2 if "two or more" in trigger_folded else 1)
            if threshold > 1:
                # WRC rules such as "2+ dive threats 2+ burst champions" are
                # alternatives. Do not double-count one enemy across two tags.
                matched = max(counts, default=0)
                if matched < threshold and example_matches < threshold:
                    continue
            else:
                matched = max(counts, default=0)
                if matched <= 0 and example_matches <= 0:
                    continue
            score += float(matched) * 3.0

        # Exact overlap with WRC's illustrative enemy draft is supporting
        # evidence, not a replacement for the textual rule.
        if example_matches:
            score += min(3, example_matches) * 2.0

        if score <= 0:
            continue
        priority = -int(row.get("priority") or 999)
        candidate = (score, priority, row)
        if best is None or candidate[:2] > best[:2]:
            best = candidate

    return best[2] if best is not None else standard


def _role_situational_rows(champion_id: str, role_ru: str, snapshot: dict | None = None) -> list[dict]:
    if snapshot is not None:
        return list(snapshot.get("role_situational", {}).get((champion_id, role_ru), []))
    return db.get_role_build_situational(champion_id, role_ru)


def _role_boot_rows(champion_id: str, role_ru: str, snapshot: dict | None = None) -> list[dict]:
    if snapshot is not None:
        return list(snapshot.get("role_boots", {}).get((champion_id, role_ru), []))
    return db.get_role_build_boots(champion_id, role_ru)


def _role_opponent_adaptation_rows(
    champion_id: str, role_ru: str, snapshot: dict | None = None,
) -> list[dict]:
    if snapshot is not None:
        return list(
            snapshot.get("role_opponent_adaptations", {}).get(
                (champion_id, role_ru), []
            )
        )
    return db.get_role_build_opponent_adaptations(champion_id, role_ru)


def _trigger_tags_from_text(value: str) -> set[str]:
    text = str(value or "").casefold()
    tags: set[str] = set()

    if any(token in text for token in ("healing", "heal", "lifesteal", "life steal", "omnivamp", "vamp", "sustain", "recovery")):
        tags.add("anti_heal")
    if any(token in text for token in ("tank", "hp stack", "health stack", "very durable", "durable", "high health")):
        tags.add("anti_tank")
    if "shield" in text:
        tags.add("anti_shield")
    if any(token in text for token in ("critical", " crit", "crit ", "crit/")):
        tags.add("anti_crit")
    if any(token in text for token in ("attack speed", "auto attack", "basic attack", "on-hit")):
        tags |= {"anti_attack_speed", "anti_auto"}
    if any(token in text for token in (
        "ap burst", "magic damage", "magical damage", "ability power",
        "vs ap", "vs magic",
    )):
        tags.add("anti_magic")
    if any(token in text for token in (
        "ad burst", "physical damage", "physical burst",
        "vs ad", "vs physical",
    )):
        tags.add("anti_physical")
    if any(token in text for token in ("crowd control", "hard cc", " cc", "tenacity")):
        tags.add("anti_cc")
    if any(token in text for token in ("mobility", "mobile", "dash", "dashes")):
        tags.add("anti_mobility")
    if any(token in text for token in ("dive", "diver", "all-in", "all in")):
        tags.add("anti_dive")
    if any(token in text for token in ("hard engage", "engage", "engages", "engaging")):
        tags.add("anti_engage")
    if "poke" in text:
        tags.add("anti_poke")
    if any(token in text for token in ("duelist", "duel", "extended fight", "prolonged fight")):
        tags.add("anti_duelist")
    if "burst" in text or "assassin" in text:
        tags.add("anti_burst")
    if any(token in text for token in ("armor/magic resist", "armor and magic resist", "resist build")):
        tags.add("anti_tank")
    return tags


def _enemy_role_build_threat_tags(enemy: dict, enemy_role: str, snapshot: dict | None = None) -> set[str]:
    """Infer only broad team threats from the enemy's own role build.

    These tags NEVER select arbitrary shop items. They merely decide which
    situational item WildRiftCore already approved for our champion+role.
    """
    row = _role_build_row(str(enemy.get("id") or ""), enemy_role, snapshot)
    if not row:
        return set()
    physical = 0
    magic = 0
    defense = 0
    tags: set[str] = set()
    for item_name in row.get("items", []) or []:
        item = _item_record(str(item_name), snapshot) or {}
        category = str(item.get("category") or "")
        if category == "Physical":
            physical += 1
        elif category == "Magic":
            magic += 1
        elif category == "Defense":
            defense += 1
        text = " ".join([
            str(item.get("stats_json") or ""),
            str(item.get("effect_en") or ""),
        ]).casefold()
        if "critical strike" in text or "crit chance" in text:
            tags.add("anti_crit")
        if any(token in text for token in ("life steal", "lifesteal", "omnivamp", "physical vamp", "magic vamp")):
            tags.add("anti_heal")

    if physical >= max(2, magic + 1):
        tags.add("anti_physical")
    elif magic >= max(2, physical + 1):
        tags.add("anti_magic")
    if defense >= 2:
        tags.add("anti_tank")
    return tags


def _enemy_threat_profile(
    enemy_objs: list[tuple[dict, str]],
    snapshot: dict | None = None,
) -> tuple[Counter, dict[str, list[str]]]:
    counts: Counter = Counter()
    enemies_by_tag: dict[str, list[str]] = defaultdict(list)

    for enemy, enemy_role in enemy_objs:
        tags = set(_direct_need_tags(_counter_items(enemy["id"], snapshot)))
        tags |= _wrc_counter_trait_tags(enemy, snapshot)
        tags |= _wrc_opponent_reason_threat_tags(enemy, snapshot)
        tags |= _wrc_example_threat_tags(enemy, snapshot)
        tags |= _generic_threat_tags(enemy, snapshot)
        if enemy_role:
            tags |= _enemy_role_build_threat_tags(enemy, enemy_role, snapshot)

        roles = {str(x).casefold() for x in enemy.get("roles", [])}
        if "tank" in roles:
            tags.add("anti_tank")
        if "marksman" in roles:
            tags |= {"anti_crit", "anti_auto"}
        if "assassin" in roles:
            tags.add("anti_burst")

        for tag in tags:
            counts[tag] += 1
            if enemy["name"] not in enemies_by_tag[tag]:
                enemies_by_tag[tag].append(enemy["name"])

    return counts, enemies_by_tag


def analyze_enemy_draft(
    enemies: list[tuple[str, str]],
    snapshot: dict | None = None,
) -> dict:
    """Public, read-only enemy profile for optional recommendation layers.

    It reuses the exact threat semantics already used by adaptive item builds,
    so experimental rune logic does not invent a second draft classifier.
    """
    raw_enemy_objs = [(_find_champ(name, snapshot), role) for name, role in enemies]
    raw_enemy_objs = [
        (enemy, enemy_role)
        for enemy, enemy_role in raw_enemy_objs
        if enemy
    ]
    enemy_objs = _infer_enemy_roles(raw_enemy_objs, snapshot) if raw_enemy_objs else []
    counts, enemies_by_tag = _enemy_threat_profile(enemy_objs, snapshot)
    return {
        "enemy_count": len(enemy_objs),
        "counts": dict(counts),
        "enemies_by_tag": {key: list(value) for key, value in enemies_by_tag.items()},
    }


def _role_rune_row(
    champion_id: str, role_ru: str, snapshot: dict | None = None,
) -> dict | None:
    if snapshot is not None:
        return (snapshot.get("role_runes") or {}).get((champion_id, role_ru))
    return None


def _role_rune_adaptation_rows(
    champion_id: str, role_ru: str, snapshot: dict | None = None,
) -> list[dict]:
    if snapshot is not None:
        return list(
            (snapshot.get("role_rune_adaptations") or {}).get(
                (champion_id, role_ru), []
            )
        )
    return []


def _recommend_runes_for_context(
    champ: dict,
    role_ru: str,
    threat_counts: Counter,
    snapshot: dict | None = None,
) -> dict | None:
    row = _role_rune_row(str(champ.get("id") or ""), role_ru, snapshot)
    if not row:
        return None

    base = [
        str(value)
        for value in (row.get("runes") or [])
        if str(value).strip()
    ]
    if not base:
        return None

    selected = list(base)
    changes: list[dict] = []
    for rule in _role_rune_adaptation_rows(
        str(champ.get("id") or ""), role_ru, snapshot
    ):
        old = str(rule.get("from_rune") or "")
        new = str(rule.get("to_rune") or "")
        condition = str(rule.get("condition_text") or "")
        if not old or not new or old not in selected or new in selected:
            continue
        tags = _trigger_tags_from_text(condition)
        if not _trigger_is_active(condition, tags, threat_counts):
            continue
        selected[selected.index(old)] = new
        changes.append({
            "from": old,
            "to": new,
            "condition": condition,
            "source": str(rule.get("source") or ""),
        })

    return {
        "base": base,
        "selected": selected,
        "changes": changes,
        "source": str(row.get("source") or ""),
        "source_url": str(row.get("source_url") or ""),
        "patch": str(row.get("patch") or ""),
        "pick_rate": row.get("pick_rate"),
        "win_rate": row.get("win_rate"),
        "rank_band": str(row.get("rank_band") or ""),
    }


def _trigger_is_active(trigger_text: str, tags: set[str], threat_counts: Counter) -> bool:
    if not tags:
        return False
    folded = str(trigger_text or "").casefold()

    # Preserve compound source rules. "Against AD burst" is not the same as
    # "against physical damage": both the damage type and a burst/assassin
    # signal must be present. The previous any-tag rule could activate Mantle
    # simply because a team had several AD champions.
    if "burst" in folded and "anti_burst" in tags:
        if int(threat_counts.get("anti_burst", 0)) <= 0:
            return False
        if (
            ("ad burst" in folded or "physical burst" in folded)
            and int(threat_counts.get("anti_physical", 0)) <= 0
        ):
            return False
        if (
            ("ap burst" in folded or "magic burst" in folded)
            and int(threat_counts.get("anti_magic", 0)) <= 0
        ):
            return False

    # WRC's generic situational rules have stable thresholds across the full
    # database. Exact opponent adaptations bypass this function and can activate
    # with a single named enemy.
    required = 1
    if (
        "2+" in folded
        or "two or more" in folded
        or "multiple" in folded
        or "heavy mobility" in folded
        or "cc chains" in folded
        or "lockdown" in folded
    ):
        required = 2
    return max(
        (int(threat_counts.get(tag, 0)) for tag in tags),
        default=0,
    ) >= required


def _source_item_score(
    trigger_text: str, tags: set[str], threat_counts: Counter,
    enemy_objs: list[tuple[dict, str]] | None = None,
) -> float:
    folded = str(trigger_text or "").casefold()

    # Exact opponent adaptations published on the champion+role page outrank
    # broad inferred tags. Multiple Opponent entries may be merged for one item.
    opponent_names = [value.strip() for value in re.findall(r"opponent:\s*([^;|]+)", folded)]
    if opponent_names and enemy_objs:
        draft_names = {
            norm_item(str(enemy.get("name") or ""))
            for enemy, _role in enemy_objs
        } | {
            norm_item(str(enemy.get("id") or ""))
            for enemy, _role in enemy_objs
        }
        if any(norm_item(name) in draft_names for name in opponent_names):
            return 100.0 + float(sum(int(threat_counts.get(tag, 0)) for tag in tags))

    if not _trigger_is_active(trigger_text, tags, threat_counts):
        return 0.0
    score = float(sum(int(threat_counts.get(tag, 0)) for tag in tags))
    if "2+" in folded:
        score += 1.0
    return score


def _approved_role_build(
    champ: dict,
    role_ru: str,
    snapshot: dict | None = None,
) -> tuple[list[str], str, list[dict], list[dict], dict | None]:
    row = _role_build_row(champ["id"], role_ru, snapshot)
    if not row:
        return [], "", [], [], None

    core = _finished_only([str(x) for x in (row.get("items") or [])], snapshot)[:5]
    baseline_boot = str(row.get("boot_name") or "")
    if baseline_boot and not _finished_item(baseline_boot, snapshot):
        baseline_boot = ""

    situational = [
        item for item in _role_situational_rows(champ["id"], role_ru, snapshot)
        if _finished_item(str(item.get("item_name") or ""), snapshot)
    ]
    boots = [
        item for item in _role_boot_rows(champ["id"], role_ru, snapshot)
        if _finished_item(str(item.get("item_name") or ""), snapshot)
        and is_boot_item(str(item.get("item_name") or ""), _item_category(str(item.get("item_name") or ""), snapshot))
    ]
    return core, baseline_boot, situational, boots, row


def recommend_build(
    champion_name: str,
    enemies: list[tuple[str, str]],
    role_ru: str = "",
    snapshot: dict | None = None,
) -> dict:
    """Source-driven champion+role build adapted to the enemy five.

    Core, situational items and boots come only from WildRiftCore's page for this
    exact champion and role. Enemy analysis chooses among those approved options;
    it never searches the global shop for an arbitrary counter-item.
    """
    champ = _find_champ(champion_name, snapshot)
    if not champ:
        raise ValueError("Чемпион не найден в локальной базе")

    effective_role = role_ru if role_ru in CANONICAL_ROLES else _default_build_role(champ)
    if effective_role and not lane_ok(champ, effective_role):
        raise ValueError("Чемпион не относится к выбранной роли")

    raw_enemy_objs = [(_find_champ(name, snapshot), role) for name, role in enemies]
    raw_enemy_objs = [(enemy, enemy_role) for enemy, enemy_role in raw_enemy_objs if enemy]
    enemy_objs = _infer_enemy_roles(raw_enemy_objs, snapshot) if raw_enemy_objs else []

    core, baseline_boot, allowed_situational, allowed_boots, source_row = _approved_role_build(
        champ, effective_role, snapshot
    )
    allowed_opponent_adaptations = _role_opponent_adaptation_rows(
        champ["id"], effective_role, snapshot
    )

    # Never invent a build from a different source when the exact
    # champion+role WildRiftCore build is missing. Production draft ranking
    # filters these pairs before they can be recommended; this guard also
    # protects direct callers from receiving an invented substitute build.
    if source_row is None or len(core) != 5 or not baseline_boot:
        raise ValueError(
            "Для выбранной роли нет полной сборки WildRiftCore"
        )

    reasons = defaultdict(list)
    reason_details = defaultdict(list)
    threat_enemies: list[str] = []
    neutral_enemies: list[str] = []

    threat_counts, enemies_by_tag = _enemy_threat_profile(enemy_objs, snapshot)
    rune_recommendation = _recommend_runes_for_context(
        champ, effective_role, threat_counts, snapshot
    )

    # WRC publishes complete source-defined variants (Standard / Vs AD / Vs AP
    # and champion-specific alternatives). Select the whole coherent variant
    # first; only then apply individual situational replacements.
    variant_rows = _role_variant_rows(champ["id"], effective_role, snapshot)
    selected_variant = _select_role_variant(
        variant_rows, threat_counts, enemy_objs
    )
    selected_variant_name = ""
    variant_core_available = False
    if selected_variant:
        selected_variant_name = str(
            selected_variant.get("variant_name") or ""
        ).strip()
        variant_items = _finished_only(
            [str(x) for x in (selected_variant.get("items") or [])],
            snapshot,
        )
        if len(variant_items) == 5:
            core = variant_items[:5]
            variant_core_available = True

    core_reason = (
        f"WildRiftCore: вариант {selected_variant_name} для роли {effective_role}"
        if selected_variant_name
        else f"WildRiftCore: стандартное ядро для роли {effective_role}"
    )
    for item in core:
        reasons[item].append(core_reason)
        reason_details[item].append({
            "kind": "variant" if selected_variant_name else "core",
            "enemy": "",
        })
    if baseline_boot:
        reasons[baseline_boot].append(f"WildRiftCore: базовые ботинки для роли {effective_role}")
        reason_details[baseline_boot].append({"kind": "core", "enemy": ""})

    scored_situational: list[tuple[float, int, str, str, set[str]]] = []

    # Highest authority: WRC's exact "Adaptations by opponent" rows for this
    # champion+role. These are explicit source recommendations and therefore
    # outrank inferred mechanic tags and generic situational rules.
    draft_enemy_keys: dict[str, str] = {}
    for enemy, _enemy_role in enemy_objs:
        display = str(enemy.get("name") or enemy.get("id") or "")
        for value in (enemy.get("id"), enemy.get("name"), enemy.get("name_ru")):
            key = db.normalize_search(str(value or ""))
            if key:
                draft_enemy_keys[key] = display

    exact_adaptation_hits: list[dict] = []
    for row in allowed_opponent_adaptations:
        enemy_key = db.normalize_search(
            str(row.get("enemy_name_norm") or row.get("enemy_name") or "")
        )
        matched_enemy = draft_enemy_keys.get(enemy_key)
        item = str(row.get("item_name") or "")
        if not matched_enemy or not item:
            continue
        reason = str(row.get("reason") or "").strip()
        trigger = (
            f"Opponent: {matched_enemy}"
            + (f"; {reason}" if reason else "")
        )
        tags = _trigger_tags_from_text(reason)
        scored_situational.append((
            200.0 + float(sum(int(threat_counts.get(tag, 0)) for tag in tags)),
            -int(row.get("priority") or 999),
            item,
            trigger,
            tags,
        ))
        exact_adaptation_hits.append({
            "enemy": matched_enemy,
            "item": item,
            "reason": reason,
        })

    for row in allowed_situational:
        item = str(row.get("item_name") or "")
        trigger = str(row.get("trigger_text") or "")
        # Exact-opponent rules are now stored structurally above. Ignore their
        # legacy merged situational copy to avoid double-scoring the same rule.
        if trigger.casefold().startswith("opponent:") and allowed_opponent_adaptations:
            continue
        tags = _trigger_tags_from_text(trigger)
        score = _source_item_score(trigger, tags, threat_counts, enemy_objs)
        if score <= 0:
            continue
        scored_situational.append((
            score,
            -int(row.get("priority") or 999),
            item,
            trigger,
            tags,
        ))
    scored_situational.sort(reverse=True)

    # Preserve at least the first three source core items. The enemy draft may
    # adapt the final two slots, but can never turn a mage into a tank or an ADC
    # into a bruiser because candidates come only from this role's WRC list.
    situational: list[str] = []
    selected_meta: dict[str, tuple[str, set[str]]] = {}
    for _score, _priority, item, trigger, tags in scored_situational:
        if item in situational:
            continue
        situational.append(item)
        selected_meta[item] = (trigger, tags)
        if len(situational) >= 2:
            break

    final_nonboots = list(core[:5])
    replace_positions = [4, 3]
    replace_cursor = 0
    for item in situational:
        trigger, tags = selected_meta[item]
        related = []
        for tag in tags:
            related.extend(enemies_by_tag.get(tag, []))
        related = list(dict.fromkeys(related))
        if related:
            threat_enemies.extend(related)
        reasons[item].append(
            trigger or "WildRiftCore: ситуационный предмет для текущего состава"
        )
        for tag in sorted(tags):
            names = enemies_by_tag.get(tag, [])
            if names:
                for enemy_name in names:
                    reason_details[item].append({"kind": tag, "enemy": enemy_name})
            else:
                reason_details[item].append({"kind": tag, "enemy": ""})

        if item in final_nonboots:
            continue
        if replace_cursor < len(replace_positions) and len(final_nonboots) > replace_positions[replace_cursor]:
            final_nonboots[replace_positions[replace_cursor]] = item
            replace_cursor += 1
        elif len(final_nonboots) < 5:
            final_nonboots.append(item)

    chosen_boot = baseline_boot
    best_boot_score = 0.0
    best_boot_trigger = ""
    best_boot_tags: set[str] = set()
    for row in allowed_boots:
        item = str(row.get("item_name") or "")
        trigger = str(row.get("trigger_text") or "")
        if not item or item == baseline_boot:
            continue
        tags = _trigger_tags_from_text(trigger)
        score = _source_item_score(trigger, tags, threat_counts, enemy_objs)
        if score > best_boot_score:
            chosen_boot = item
            best_boot_score = score
            best_boot_trigger = trigger
            best_boot_tags = tags

    if chosen_boot and chosen_boot != baseline_boot:
        situational.append(chosen_boot)
        reasons[chosen_boot].append(
            best_boot_trigger or "WildRiftCore: альтернативные ботинки для текущего состава"
        )
        for tag in sorted(best_boot_tags):
            for enemy_name in enemies_by_tag.get(tag, []):
                reason_details[chosen_boot].append({"kind": tag, "enemy": enemy_name})

    ordered = _finished_only(final_nonboots, snapshot)
    if chosen_boot and _finished_item(chosen_boot, snapshot):
        ordered.append(chosen_boot)
    ordered = list(dict.fromkeys(ordered))[:6]

    threat_set = set(threat_enemies)
    for enemy, _enemy_role in enemy_objs:
        if enemy["name"] not in threat_set:
            neutral_enemies.append(enemy["name"])

    base = list(core)
    if baseline_boot:
        base.append(baseline_boot)

    return {
        "champion": champ,
        "role": effective_role,
        "base": base,
        "situational": list(dict.fromkeys(situational)),
        "ordered": ordered,
        "reasons": {key: list(dict.fromkeys(values)) for key, values in reasons.items()},
        "reason_details": {
            key: list({
                (detail.get("kind", ""), detail.get("enemy", "")): detail
                for detail in values
            }.values())
            for key, values in reason_details.items()
        },
        "threat_enemies": list(dict.fromkeys(threat_enemies)),
        "neutral_enemies": neutral_enemies,
        "pool_size": (
            len(core)
            + len(allowed_situational)
            + len(allowed_opponent_adaptations)
            + len(allowed_boots)
        ),
        "source": str((source_row or {}).get("source") or ""),
        "source_url": str((source_row or {}).get("source_url") or ""),
        "source_missing": source_row is None,
        "selected_variant": selected_variant_name,
        "selected_variant_core_available": variant_core_available,
        "selected_variant_trigger": str(
            (selected_variant or {}).get("trigger_text") or ""
        ),
        "selected_variant_example_enemies": list(
            (selected_variant or {}).get("example_enemies") or []
        ),
        "selected_variant_example_text": str(
            (selected_variant or {}).get("example_text") or ""
        ),
        "exact_opponent_adaptations": exact_adaptation_hits,
        "threat_counts": dict(threat_counts),
        "draft_features": DRAFT_MATRIX.feature_vector(threat_counts),
        "runes": rune_recommendation,
    }

