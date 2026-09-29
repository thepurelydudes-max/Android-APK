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


def _line_weight(my_role: str, enemy_role: str) -> float:
    """Weight the likely lane opponent without excluding the rest of the draft."""
    if not enemy_role:
        return 1.0
    if my_role in {"Барон", "Мид"}:
        return 2.0 if enemy_role == my_role else 1.0
    if my_role == "Лес":
        return 1.5 if enemy_role == "Лес" else 1.0
    if my_role in {"ADC", "Саппорт"}:
        return 1.5 if enemy_role in {"ADC", "Саппорт"} else 1.0
    return 1.0


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


def recommend_picks(role_ru: str, enemies: list[tuple[str, str]], limit: int = 8, snapshot: dict | None = None) -> list[dict]:
    """Rank legal role candidates against the complete entered enemy draft.

    DraftMatrixEngine is the single scoring authority:
      60% role-correct matchup matrix strength
      20% weighted multi-target coverage
      15% current role tier
       5% current role win-rate percentile

    The likely lane opponent receives extra weight, but every enemy remains in
    the matrix. The final visible score is the ranking authority; secondary
    fields below are deterministic tie-breakers only.
    """
    raw_enemy_objs: list[tuple[dict, str]] = []
    for name, enemy_role in enemies:
        champ = _find_champ(name, snapshot)
        if champ:
            raw_enemy_objs.append((champ, enemy_role))
    if not raw_enemy_objs:
        return []

    enemy_objs = _infer_enemy_roles(raw_enemy_objs, snapshot)
    enemy_ids = {enemy["id"] for enemy, _enemy_role in enemy_objs if enemy.get("id")}
    stat_lane = ROLE_TO_STAT.get(role_ru, "")

    candidate_rows: list[tuple[dict, str, dict | None]] = []
    for cand in (snapshot.get("champions", []) if snapshot is not None else db.champions()):
        if cand.get("id") in enemy_ids:
            continue
        tier = _tier(cand.get("id", ""), role_ru, snapshot)
        st = _stat(cand.get("id", ""), stat_lane, "all", snapshot) if stat_lane else None
        # Selected role is a hard eligibility gate. Niche/noisy stat rows can
        # rank a legal flex pick but can never invent a role for that champion.
        if not lane_ok(cand, role_ru):
            continue
        # Production recommendations must have a complete WildRiftCore build
        # for this exact champion+role. This keeps the old WR Pocket fallback
        # unreachable from normal draft recommendations.
        if snapshot is None or snapshot.get("role_builds"):
            row = _role_build_row(cand.get("id", ""), role_ru, snapshot)
            healthy_core = (
                _finished_only([str(x) for x in (row.get("items") or [])], snapshot)
                if row else []
            )
            if row is None or len(healthy_core) < 3:
                continue
        candidate_rows.append((cand, tier, st))

    role_win_rates: list[float] = []
    for _cand, _tier_value, st in candidate_rows:
        if st and st.get("win_rate") is not None:
            try:
                role_win_rates.append(float(st["win_rate"]))
            except (TypeError, ValueError):
                pass

    out = []
    for cand, tier, st in candidate_rows:
        matrix_edges: list[DraftEdge] = []
        mirror_edge: float | None = None

        for enemy, inferred_role in enemy_objs:
            edge = float(_matchup_score(cand["id"], enemy["id"], role_ru, snapshot))
            edge = DRAFT_MATRIX.clamp_edge(edge)
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

        direct_lane_edges = matrix["direct_lane_edges"]
        lane_hard_loss = any(edge <= -2.0 for edge in direct_lane_edges)

        out.append({
            "champion": cand,
            "score": final["score"],
            "positive": matrix["positive"],
            "negative": matrix["negative"],
            "neutral": matrix["neutral"],
            "positive_strength": matrix["positive_strength"],
            "negative_strength": matrix["negative_strength"],
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

    out.sort(
        key=lambda x: (
            x["score"],
            x["matchup_score"],
            x["coverage_score"],
            TIER_ORDER.get(x.get("tier", ""), 0),
            x["winrate_score"],
            not x.get("lane_hard_loss", False),
            x["positive_strength"],
            -x["negative_strength"],
        ),
        reverse=True,
    )
    return out[:limit]

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


def _primary_offense_category(champ: dict, pool: list[dict], role_ru: str, snapshot: dict | None = None) -> str:
    counts = _pool_category_counts(pool, snapshot)
    roles = {str(x).casefold() for x in champ.get("roles", [])}
    physical = int(counts.get("Physical", 0))
    magic = int(counts.get("Magic", 0))

    if not physical:
        return "Magic" if magic else ""
    if not magic:
        return "Physical"

    physical_score = physical
    magic_score = magic
    if "marksman" in roles:
        physical_score += 5
    if "fighter" in roles:
        physical_score += 2
    if "assassin" in roles:
        physical_score += 2
    if "mage" in roles:
        magic_score += 5
    if role_ru == "ADC":
        physical_score += 3
    if role_ru == "Мид" and "mage" in roles:
        magic_score += 2

    return "Physical" if physical_score >= magic_score else "Magic"


def _build_profile(champ: dict, pool: list[dict], role_ru: str, snapshot: dict | None = None) -> dict:
    return {
        "roles": {str(x).casefold() for x in champ.get("roles", [])},
        "lanes": {str(x).casefold() for x in champ.get("lanes", [])},
        "counts": _pool_category_counts(pool, snapshot),
        "offense": _primary_offense_category(champ, pool, role_ru, snapshot),
    }


def _core_category_quotas(champ: dict, pool: list[dict], role_ru: str, snapshot: dict | None = None) -> tuple[list[tuple[str, int]], dict]:
    """Choose five non-boot slots without treating Build Trends as a ready build."""
    profile = _build_profile(champ, pool, role_ru, snapshot)
    roles = profile["roles"]
    lanes = profile["lanes"]
    counts = profile["counts"]
    offense = profile["offense"]
    quotas: list[tuple[str, int]] = []

    def add(category: str, count: int) -> None:
        if not category or count <= 0:
            return
        available = int(counts.get(category, 0))
        if available:
            quotas.append((category, min(count, available)))

    if role_ru == "Саппорт":
        if "assassin" in roles or "marksman" in roles:
            defense_slots = min(1, int(counts.get("Defense", 0)))
            add(offense, 5 - defense_slots)
            add("Defense", defense_slots)
        elif "tank" in roles or (
            int(counts.get("Defense", 0)) > max(int(counts.get("Magic", 0)), int(counts.get("Support", 0)))
            and "mage" not in roles
        ):
            support_slots = min(2, int(counts.get("Support", 0)))
            defense_slots = min(5 - support_slots, int(counts.get("Defense", 0)))
            add("Defense", defense_slots)
            add("Support", support_slots)
            add(offense, 5 - sum(value for _category, value in quotas))
        else:
            flex_lane = bool(lanes & {"mid", "top", "jungle", "ad", "adc"})
            support_count = int(counts.get("Support", 0))
            magic_count = int(counts.get("Magic", 0))
            if support_count >= 4 and not flex_lane and support_count >= magic_count:
                add("Support", 5)
            elif support_count >= 3:
                support_slots = 2 if magic_count >= support_count * 2 else 3
                add("Support", support_slots)
                add(offense, 5 - sum(value for _category, value in quotas))
            elif support_count >= 1:
                # Even flex mages should keep at least one genuine support item
                # when the user explicitly selected Support. Champion-wide Build
                # Trends otherwise let the Mid damage pool completely erase the
                # selected role and can push pure penetration into the core.
                add("Support", 1)
                add(offense, 4)
            else:
                add(offense, 5)
    else:
        if "tank" in roles:
            defense_slots = min(3, int(counts.get("Defense", 0)))
            add(offense, 5 - defense_slots)
            add("Defense", defense_slots)
        elif "fighter" in roles:
            defense_slots = min(2, int(counts.get("Defense", 0)))
            add(offense, 5 - defense_slots)
            add("Defense", defense_slots)
        else:
            add(offense, 5)
            add("Defense", 5 - sum(value for _category, value in quotas))

    return quotas, profile


def _core_item_penalty(item_name: str) -> int:
    tags = tags_for(item_name)
    # Pure penetration / anti-tank tools are the most situational. Anti-heal,
    # anti-shield and defensive answers are still situational, but may be valid
    # earlier when the normal pool is small.
    if tags & {"anti_tank", "anti_armor", "anti_magic_resist"}:
        return 2
    return 1 if tags & CORE_SITUATIONAL_TAGS else 0


def _finished_boot_catalog(snapshot: dict | None = None) -> list[str]:
    if snapshot is not None:
        rows = list((snapshot.get("items") or {}).values())
    else:
        rows = db.item_catalog_rows()
    out: list[str] = []
    for row in rows:
        name = str(row.get("name") or "")
        category = str(row.get("category") or "")
        if not name or not is_boot_item(name, category):
            continue
        if str(row.get("tier") or "").strip().casefold() != "upgraded":
            continue
        out.append(name)
    return out


def _fallback_boot(champ: dict, role_ru: str, snapshot: dict | None = None) -> str:
    """Return a conservative finished boot when a source pool lost its boot row.

    This is a safety net only. Under normal conditions the champion's own WR
    Pocket pool decides the boot. The fallback prevents a data-refresh/parser
    hiccup from silently producing a five-item build.
    """
    available = _finished_boot_catalog(snapshot)
    if not available:
        return ""
    have = {norm_item(name): name for name in available}
    roles = {str(x).casefold() for x in champ.get("roles", [])}
    preferences: list[str]
    if role_ru == "Саппорт":
        preferences = ["Crimson Lucidity", "Chainlaced Crushers", "Armored Advance", "Spellslinger's Shoes"]
    elif "mage" in roles:
        preferences = ["Spellslinger's Shoes", "Crimson Lucidity", "Chainlaced Crushers", "Armored Advance"]
    elif "marksman" in roles:
        preferences = ["Gunmetal Greaves", "Armorcrusher Boots", "Immortal Boots", "Crimson Lucidity"]
    elif "tank" in roles or "fighter" in roles:
        preferences = ["Chainlaced Crushers", "Armored Advance", "Immortal Boots", "Crimson Lucidity"]
    else:
        preferences = ["Crimson Lucidity", "Chainlaced Crushers", "Armored Advance", "Spellslinger's Shoes"]
    for wanted in preferences:
        hit = have.get(norm_item(wanted))
        if hit:
            return hit
    return available[0]


def _core_build(champ: dict, pool: list[dict], role_ru: str, snapshot: dict | None = None) -> list[str]:
    quotas, profile = _core_category_quotas(champ, pool, role_ru, snapshot)
    grouped: dict[str, list[dict]] = defaultdict(list)

    for row in pool:
        category = _semantic_pool_category(row, snapshot)
        if category != "Boots":
            grouped[category].append(row)

    for rows in grouped.values():
        rows.sort(
            key=lambda row: (
                _core_item_penalty(str(row.get("item_name") or "")),
                int(row.get("priority") or 999),
                str(row.get("item_name") or ""),
            )
        )

    picked: list[str] = []
    for category, amount in quotas:
        for row in grouped.get(category, [])[:amount]:
            name = str(row.get("item_name") or "")
            if name and name not in picked:
                picked.append(name)

    preferred_categories = [category for category, _amount in quotas]
    for category in (profile.get("offense"), "Support", "Defense", "Physical", "Magic", "Other"):
        if category and category not in preferred_categories:
            preferred_categories.append(category)

    fallback: list[tuple[int, int, int, str]] = []
    for category_index, category in enumerate(preferred_categories):
        for row in grouped.get(category, []):
            name = str(row.get("item_name") or "")
            if not name:
                continue
            fallback.append(
                (
                    _core_item_penalty(name),
                    category_index,
                    int(row.get("priority") or 999),
                    name,
                )
            )

    for _penalty, _category_index, _priority, name in sorted(fallback):
        if len(picked) >= 5:
            break
        if name not in picked:
            picked.append(name)

    boots = [row for row in pool if _semantic_pool_category(row, snapshot) == "Boots"]
    boots.sort(key=lambda row: int(row.get("priority") or 999))
    boot_name = str(boots[0].get("item_name") or "") if boots else ""
    if not boot_name:
        boot_name = _fallback_boot(champ, role_ru, snapshot)
    if boot_name and boot_name not in picked:
        picked.append(boot_name)

    return picked


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


def _compatible(item: str, champ: dict, pool: list[dict], role_ru: str, snapshot: dict | None = None) -> bool:
    """Allow situational items only when they still fit the selected champion."""
    pool_names = {norm_item(str(row.get("item_name") or "")) for row in pool}
    category = _item_category(item, snapshot)
    tags = tags_for(item)

    # WR Pocket pools are champion-wide, not role-specific. A flex mage's Mid
    # damage pool must not leak pure penetration purchases into a Support build.
    if role_ru == "Саппорт" and category in {"Physical", "Magic"} and tags & {"anti_tank", "anti_armor", "anti_magic_resist"}:
        return False

    if norm_item(item) in pool_names:
        return True
    profile = _build_profile(champ, pool, role_ru, snapshot)
    roles = profile["roles"]
    counts = profile["counts"]

    if category in {"Physical", "Magic"}:
        return False
    if category == "Boots" or "boots" in tags or "universal" in tags:
        return True
    if category == "Defense":
        return (
            "tank" in roles
            or (("fighter" in roles or role_ru == "Саппорт") and int(counts.get("Defense", 0)) >= 3)
        )
    if category == "Support":
        return role_ru == "Саппорт" and "support" in roles
    return False


def _default_build_role(champ: dict) -> str:
    for role in CANONICAL_ROLES:
        if lane_ok(champ, role):
            return role
    return ""


def _adaptation_severity(edge: float) -> float:
    return 1.0 + 0.5 * max(0.0, min(3.0, -float(edge)))


def enforce_single_boot_rule(base: list[str], situational: list[str], adaptation_scores=None) -> tuple[list[str], list[str]]:
    """Keep at most one boot item across normal and adaptive recommendations."""
    base = list(dict.fromkeys(base or []))
    situational = list(dict.fromkeys(situational or []))
    scores = adaptation_scores or {}
    boot_names = [name for name in [*base, *situational] if is_boot_item(name)]
    if len(boot_names) <= 1:
        return base, situational

    situational_boots = [name for name in situational if is_boot_item(name)]
    if situational_boots:
        chosen = max(situational_boots, key=lambda name: float(scores.get(name, 0.0) or 0.0))
    else:
        chosen = next(name for name in base if is_boot_item(name))

    base_out = [name for name in base if not is_boot_item(name)]
    situational_out = [name for name in situational if not is_boot_item(name)]
    if chosen in situational:
        situational_out.append(chosen)
    else:
        base_out.append(chosen)
    return base_out, situational_out


def order_build_items(base: list[str], situational: list[str], pool: list[dict], adaptation_scores) -> list[str]:
    """Keep the coherent core and replace only low-priority tail slots."""
    scores = adaptation_scores or {}
    final = list(dict.fromkeys(base or []))

    for item in situational or []:
        if item in final:
            continue
        if is_boot_item(item):
            replaced = False
            for index, current in enumerate(final):
                if is_boot_item(current):
                    final[index] = item
                    replaced = True
                    break
            if not replaced and len(final) < 6:
                final.append(item)
            continue

        non_boot_indexes = [index for index, current in enumerate(final) if not is_boot_item(current)]
        if not non_boot_indexes:
            continue
        replaceable = non_boot_indexes[2:] or non_boot_indexes[-1:]
        replace_index = min(
            replaceable,
            key=lambda index: (float(scores.get(final[index], 0.0) or 0.0), -index),
        )
        final[replace_index] = item

    out: list[str] = []
    seen_boot = False
    for item in final:
        if item in out:
            continue
        if is_boot_item(item):
            if seen_boot:
                continue
            seen_boot = True
        out.append(item)
        if len(out) >= 6:
            break
    return out


def _ensure_exactly_one_boot(
    names: list[str], champ: dict, role_ru: str, snapshot: dict | None = None,
) -> list[str]:
    """Final invariant: every non-empty six-slot recommendation has one boot."""
    out = list(dict.fromkeys(names or []))
    boots = [name for name in out if is_boot_item(name, _item_category(name, snapshot))]
    if boots:
        keep = boots[0]
        out = [name for name in out if not is_boot_item(name, _item_category(name, snapshot)) or name == keep]
    else:
        keep = _fallback_boot(champ, role_ru, snapshot)
        if keep:
            if len(out) >= 6:
                # Never evict the first two identity/core items just to restore
                # a missing data-source boot. Replace the tail instead.
                replace_at = max(2, len(out) - 1)
                out[replace_at] = keep
            else:
                out.append(keep)
    return list(dict.fromkeys(out))[:6]


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


def _fallback_pool_build(
    champ: dict,
    enemy_objs: list[tuple[dict, str]],
    effective_role: str,
    snapshot: dict | None = None,
) -> dict:
    """Restore the proven item-pool builder when role-build data is unavailable.

    WildRiftCore champion+role builds remain the preferred source. This fallback
    is deliberately narrow: it uses only this champion's existing WR Pocket item
    pool plus the already-known counter signals. It never searches the global
    shop, so a temporary parser/rate-limit failure cannot produce six blank slots
    and cannot turn a mage/ADC into an unrelated defensive build.
    """
    pool = _item_pool(champ["id"], snapshot)
    pool_names = {norm_item(str(row.get("item_name") or "")) for row in pool}
    base = _finished_only(_core_build(champ, pool, effective_role, snapshot), snapshot)

    reasons = defaultdict(list)
    reason_details = defaultdict(list)
    scores: Counter = Counter()
    threat_enemies: list[str] = []
    neutral_enemies: list[str] = []

    for item in base:
        reasons[item].append("резервное ядро из актуального пула предметов героя")
        reason_details[item].append({"kind": "core", "enemy": ""})

    for enemy, _enemy_role in enemy_objs:
        edge = float(_matchup_score(champ["id"], enemy["id"], effective_role, snapshot))
        severity = _adaptation_severity(edge)
        if edge < 0:
            threat_enemies.append(enemy["name"])
        else:
            neutral_enemies.append(enemy["name"])

        direct = _counter_items(enemy["id"], snapshot)
        need_tags = _direct_need_tags(direct)

        for row in direct:
            item = str(row.get("item_name") or "")
            if not item or not _compatible(item, champ, pool, effective_role, snapshot):
                continue
            source_fit = 1.0 if norm_item(item) in pool_names else 0.65
            scores[item] += 3.0 * severity * source_fit
            reasons[item].append(f"против {enemy['name']}")
            reason_details[item].append({"kind": "direct", "enemy": enemy["name"]})

        for row in pool:
            item = str(row.get("item_name") or "")
            hits = tags_for(item) & need_tags
            if hits:
                scores[item] += 1.5 * severity * len(hits)
                for tag in sorted(hits):
                    reasons[item].append(f"{tag} против {enemy['name']}")
                    reason_details[item].append({"kind": tag, "enemy": enemy["name"]})

        # This is the same restrained adaptation layer that existed before the
        # role-build rewrite. It only promotes items already approved for the
        # champion by the pool and therefore behaves like a practical
        # matchup-aware build assistant rather than a global-shop generator.
        generic_tags = _generic_threat_tags(enemy, snapshot)
        for row in pool:
            item = str(row.get("item_name") or "")
            hits = tags_for(item) & generic_tags
            if not hits:
                continue
            scores[item] += 0.55 * severity * len(hits)
            for tag in sorted(hits):
                reason_details[item].append({"kind": tag, "enemy": enemy["name"]})

    max_adaptations = 3 if len(enemy_objs) >= 4 else 2
    ranked_adaptations = [
        (item, float(score))
        for item, score in scores.most_common()
        if float(score) >= 2.5
    ][:max_adaptations]
    situational = _finished_only([item for item, _score in ranked_adaptations], snapshot)

    base, situational = enforce_single_boot_rule(base, situational, scores)
    base = _finished_only(base, snapshot)
    situational = _finished_only(situational, snapshot)
    ordered = _finished_only(order_build_items(base, situational, pool, scores), snapshot)
    ordered = _ensure_exactly_one_boot(ordered, champ, effective_role, snapshot)
    ordered = list(dict.fromkeys(ordered))[:6]

    return {
        "champion": champ,
        "role": effective_role,
        "base": base,
        "situational": situational,
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
        "neutral_enemies": list(dict.fromkeys(neutral_enemies)),
        "pool_size": len(pool),
        "source": "wrpocket.app:fallback",
        "source_url": "",
        "source_missing": True,
        "fallback_used": True,
        "threat_counts": {},
    }


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
    # protects direct callers from silently entering the legacy fallback.
    if source_row is None or len(core) < 3:
        raise ValueError(
            "Для выбранной роли нет полной сборки WildRiftCore"
        )

    reasons = defaultdict(list)
    reason_details = defaultdict(list)
    threat_enemies: list[str] = []
    neutral_enemies: list[str] = []

    threat_counts, enemies_by_tag = _enemy_threat_profile(enemy_objs, snapshot)

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
    }

