from __future__ import annotations

from collections import Counter, defaultdict
import re

import db
from draft_matrix_engine import DraftEdge, DraftMatrixEngine

ROLE_TO_LANES = {
    "EXP": {"exp"},
    "Лес": {"jungle"},
    "Мид": {"mid"},
    "Голд": {"gold"},
    "Роум": {"roam"},
}
ROLE_TO_STAT = {"EXP": "exp", "Лес": "jungle", "Мид": "mid", "Голд": "gold", "Роум": "roam"}

# Same normalized recommendation model as the Wild Rift project.  The draft
# itself is the main signal: matchup quality + multi-target coverage = 80%.
# MLBB tier data is cached from MLBBDex /api/v1/rankings during database update.
# If that provider is temporarily unavailable, the last known-good tier remains
# in SQLite; heroes without a tier fall back to the neutral score of 50.
FINAL_WEIGHTS = {
    "matchup": 0.60,
    "coverage": 0.20,
    "tier": 0.15,
    "winrate": 0.05,
}

TIER_SCORE = {"S+": 100.0, "S": 80.0, "A": 60.0, "B": 40.0, "C": 20.0, "D": 0.0}
TIER_ORDER = {"S+": 6, "S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}
CANONICAL_ROLES = ("EXP", "Лес", "Мид", "Голд", "Роум")

DRAFT_MATRIX = DraftMatrixEngine()

# MLBB matchup rows use a ±1.5 scale, while the Wild Rift engine uses ±3.
# Normalize both to -1..+1 before weighting so the resulting 0..100 score has
# the same meaning in both applications.
MATCHUP_ABS_MAX = 1.5
HARD_MATCHUP_THRESHOLD = MATCHUP_ABS_MAX * (2.0 / 3.0)

# A matchup must improve the candidate's win rate by at least +2 percentage
# points before the UI calls it a real counter. Smaller positive/negative edges
# still participate fully in the recommendation score.
COUNTER_LABEL_THRESHOLD_PP = 2.0
DEFAULT_MATCHUP_SCALE_PP_P95 = 2.5


def _counter_label_threshold(snapshot: dict | None = None) -> float:
    scale_pp = 0.0
    if snapshot is not None:
        try:
            scale_pp = float(snapshot.get("matchup_edge_scale_pp_p95") or 0.0)
        except (TypeError, ValueError):
            scale_pp = 0.0
    else:
        try:
            scale_pp = float(db.get_meta("matchup_edge_scale_pp_p95", "") or 0.0)
        except (TypeError, ValueError):
            scale_pp = 0.0
    if scale_pp <= 0:
        scale_pp = DEFAULT_MATCHUP_SCALE_PP_P95
    return max(
        0.0,
        min(MATCHUP_ABS_MAX, (COUNTER_LABEL_THRESHOLD_PP / scale_pp) * MATCHUP_ABS_MAX),
    )

ITEM_TAGS = {
    "Dominance Ice": {"anti_heal", "anti_shield", "anti_physical", "anti_magic", "defense", "tank", "universal"},
    "Sea Halberd": {"anti_heal", "physical", "marksman", "fighter", "assassin"},
    "Glowing Wand": {"anti_heal", "magic", "mage"},
    "Athena's Shield": {"anti_magic", "anti_burst", "defense", "tank", "universal"},
    "Radiant Armor": {"anti_magic", "defense", "tank", "universal"},
    "Blade Armor": {"anti_crit", "anti_auto", "anti_physical", "defense", "tank", "universal"},
    "Antique Cuirass": {"anti_physical", "anti_burst", "defense", "tank", "universal"},
    "Chastise Pauldron": {"anti_attack_speed", "anti_auto", "anti_physical", "defense", "tank", "universal"},
    "Twilight Armor": {"anti_burst", "defense", "tank", "universal"},
    "Immortality": {"anti_burst", "defense", "universal"},
    "Queen's Wings": {"anti_burst", "physical", "fighter", "defense"},
    "Rose Gold Meteor": {"anti_magic", "anti_burst", "physical", "marksman", "fighter", "assassin"},
    "Winter Crown": {"anti_burst", "magic", "universal"},
    "Wind of Nature": {"anti_physical", "anti_auto", "physical", "marksman"},
    "Malefic Roar": {"anti_tank", "anti_armor", "physical", "marksman", "fighter", "assassin"},
    "Demon Hunter Sword": {"anti_tank", "physical", "marksman", "fighter"},
    "Divine Glaive": {"anti_tank", "anti_magic_resist", "magic", "mage"},
    "Wishing Lantern": {"anti_tank", "magic", "mage"},
    "Tough Boots": {"anti_magic", "anti_cc", "boots", "defense", "universal"},
    "Warrior Boots": {"anti_physical", "anti_auto", "boots", "defense", "universal"},
    "Magic Shoes": {"boots", "universal"},
    "Arcane Boots": {"boots", "magic", "mage"},
    "Swift Boots": {"boots", "physical", "marksman"},
    "Demon Shoes": {"boots", "universal"},
    "Rapid Boots": {"boots", "universal"},
}


# Source names vary slightly by apostrophe/case.
def norm_item(s: str) -> str:
    s = s.replace("’", "'").strip().casefold()
    return re.sub(r"\s+", " ", s)




def is_boot_item(name: str, category: str = "") -> bool:
    text = norm_item(name)
    cat = str(category or "").casefold()
    return "boot" in text or "boot" in cat or "boots" in cat

def tags_for(item: str) -> set[str]:
    n = norm_item(item)
    for k, tags in ITEM_TAGS.items():
        if norm_item(k) == n:
            return set(tags)
    return set()


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
        # Never leak a different role's matchup into the selected role.  MLBB's
        # current source stores general rows (role=''), which are valid fallback.
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
    """Return the latest cached MLBB meta tier for the selected lane."""
    if snapshot is not None:
        tier = str(snapshot.get("tiers", {}).get((champion_id, role_ru), "") or "").strip().upper()
        if tier in TIER_SCORE:
            return tier
    else:
        try:
            tier = str(db.get_champion_tier(champion_id, role_ru, "mlbbdex") or "").strip().upper()
            if tier in TIER_SCORE:
                return tier
        except Exception:
            pass
    # Compatibility with databases produced by MLCA 1.x.
    stat_lane = ROLE_TO_STAT.get(role_ru, "")
    if not stat_lane:
        return ""
    st = _stat(champion_id, stat_lane, "all", snapshot)
    tier = str((st or {}).get("tier") or "").strip().upper()
    return tier if tier in TIER_SCORE else ""


def _role_evidence(champ: dict, role_ru: str, snapshot: dict | None = None) -> float | None:
    """Return evidence that *champ* is actually played in ``role_ru``."""
    allowed = ROLE_TO_LANES.get(role_ru, set())
    lanes = {str(x).casefold() for x in champ.get("lanes", [])}
    lane_known = bool(lanes & allowed)
    stat_lane = ROLE_TO_STAT.get(role_ru, "")
    st = _stat(champ.get("id", ""), stat_lane, "all", snapshot) if stat_lane else None

    if not lane_known and not st:
        return None

    pick_rate = 0.0
    if st and st.get("pick_rate") is not None:
        try:
            pick_rate = max(0.0, float(st.get("pick_rate") or 0.0))
        except (TypeError, ValueError):
            pick_rate = 0.0

    return pick_rate + (2.0 if lane_known else 0.0)


def _infer_enemy_roles(enemy_objs: list[tuple[dict, str]], snapshot: dict | None = None) -> list[tuple[dict, str]]:
    """Infer likely MLBB lanes for an enemy draft using local role data."""
    from itertools import permutations

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

    return [(champ, role) for champ, role in result]


def _line_weight(my_role: str, enemy_role: str) -> float:
    """Weight the likely lane opponent without excluding the rest of the draft."""
    if not enemy_role:
        return 1.0
    if my_role in {"EXP", "Мид"}:
        return 2.0 if enemy_role == my_role else 1.0
    if my_role == "Лес":
        return 1.5 if enemy_role == "Лес" else 1.0
    if my_role in {"Голд", "Роум"}:
        return 1.5 if enemy_role in {"Голд", "Роум"} else 1.0
    return 1.0


def _winrate_percentile(win_rate: float | None, population: list[float]) -> float:
    """Map role win rate to a 0..100 percentile, matching the WR engine."""
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
            wanted = norm_item(item_name)
            row = next((v for k, v in items.items() if norm_item(k) == wanted), None)
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


def _role_build_row(champion_id: str, role_ru: str, snapshot: dict | None = None) -> dict | None:
    if not role_ru:
        return None
    if snapshot is not None:
        row = snapshot.get("role_builds", {}).get((champion_id, role_ru))
        return dict(row) if row else None
    return db.get_role_build(champion_id, role_ru, "mlbb.rone")


def _has_valid_role_build(champion_id: str, role_ru: str, snapshot: dict | None = None) -> bool:
    """A hero may be recommended for a lane only with a real downloaded lane core."""
    row = _role_build_row(champion_id, role_ru, snapshot)
    if not row:
        return False
    items = [
        str(name) for name in (row.get("items") or [])
        if str(name).strip() and _finished_item(str(name), snapshot)
    ]
    boot = str(row.get("boot_name") or "").strip()
    if boot and not _finished_item(boot, snapshot):
        boot = ""
    # Academy's measured top-three footprint may be either 3 non-boot items or
    # 2 non-boot items + boots. Both are real lane evidence; the flexible tail
    # is filled later from the downloaded champion pool.
    return len(items) >= 3 or (len(items) >= 2 and bool(boot))


def recommend_picks(role_ru: str, enemies: list[tuple[str, str]], limit: int = 8, snapshot: dict | None = None) -> list[dict]:
    """Rank MLBB heroes using the same draft-matrix model as WRCA."""
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
        if not _has_valid_role_build(str(cand.get("id") or ""), role_ru, snapshot):
            continue
        tier = _tier(cand.get("id", ""), role_ru, snapshot)
        st = _stat(cand.get("id", ""), stat_lane, "all", snapshot) if stat_lane else None
        if not lane_ok(cand, role_ru) and not st and not tier:
            continue
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
        for enemy, inferred_role in enemy_objs:
            edge = float(_matchup_score(cand["id"], enemy["id"], role_ru, snapshot))
            matrix_edges.append(DraftEdge(
                enemy_id=str(enemy["id"]),
                enemy_name=str(enemy["name"]),
                enemy_role=str(inferred_role or ""),
                edge=edge,
                weight=DRAFT_MATRIX.lane_weight(role_ru, inferred_role),
            ))
        analysis = DRAFT_MATRIX.analyze_row(
            matrix_edges,
            counter_threshold=_counter_label_threshold(snapshot),
        )

        wr = None
        if st and st.get("win_rate") is not None:
            try:
                wr = float(st["win_rate"])
            except (TypeError, ValueError):
                pass
        winrate_score = _winrate_percentile(wr, role_win_rates)
        tier_score = TIER_SCORE.get(tier, 50.0)
        components = DRAFT_MATRIX.final_score(
            matchup_score=analysis["matchup_score"],
            coverage_score=analysis["coverage_score"],
            tier_score=tier_score,
            winrate_score=winrate_score,
        )
        lane_hard_loss = any(
            edge <= -HARD_MATCHUP_THRESHOLD for edge in analysis["direct_lane_edges"]
        )

        out.append({
            "champion": cand,
            "score": components["score"],
            "positive": analysis["positive"],
            "negative": analysis["negative"],
            "neutral": analysis["neutral"],
            "positive_strength": analysis["positive_strength"],
            "negative_strength": analysis["negative_strength"],
            "hard_counters": analysis["hard_counters"],
            "coverage_count": analysis["coverage_count"],
            "coverage_total": analysis["coverage_total"],
            "coverage_score": analysis["coverage_score"],
            "coverage_bonus": components["coverage"],
            "tier": tier,
            "tier_score": tier_score,
            "tier_bonus": components["tier"],
            "win_rate": wr,
            "winrate_score": winrate_score,
            "matchup_score": analysis["matchup_score"],
            "matchup_contribution": components["matchup"],
            "winrate_bonus": components["winrate"],
            "lane_hard_loss": lane_hard_loss,
            "enemy_roles": analysis["enemy_roles"],
            "matrix_row": analysis["matrix_row"],
        })

    out.sort(
        key=lambda x: (
            not x.get("lane_hard_loss", False),
            x["score"],
            x["matchup_score"],
            x["coverage_score"],
            TIER_ORDER.get(x.get("tier", ""), 0),
            x["winrate_score"],
            x["positive_strength"],
            -x["negative_strength"],
        ),
        reverse=True,
    )
    return out[:limit]


def _generic_threat_tags(enemy: dict) -> set[str]:
    roles = {str(x).casefold() for x in enemy.get("roles", [])}
    specialties = {str(x).casefold() for x in enemy.get("specialties", [])}
    typ = str(enemy.get("damage_type", "")).casefold()
    tags = set()
    if "marksman" in roles:
        tags |= {"anti_physical", "anti_crit", "anti_auto", "anti_attack_speed"}
    if "assassin" in roles:
        tags.add("anti_burst")
    if "mage" in roles or "ap" in typ or "magic" in typ:
        tags.add("anti_magic")
    if "tank" in roles:
        tags.add("anti_tank")
    if "support" in roles:
        tags.add("anti_cc")
    if "fighter" in roles and "ap" not in typ and "magic" not in typ:
        tags.add("anti_physical")
    if specialties & {"regen", "heal", "healing", "sustain"}:
        tags |= {"anti_heal", "anti_shield"}
    if specialties & {"burst", "reap"}:
        tags.add("anti_burst")
    if specialties & {"control", "crowd control", "cc", "initiator"}:
        tags.add("anti_cc")
    return tags


def _compatible(item: str, pool_names: set[str], cand_arch: set[str], has_pool: bool) -> bool:
    n = norm_item(item)
    if n in pool_names:
        return True
    tags = tags_for(item)
    if not has_pool:
        if "magic" in tags and "magic" not in cand_arch and "mage" not in cand_arch:
            return False
        if {"marksman", "assassin", "fighter"} & tags and not ({"marksman", "assassin", "fighter"} & cand_arch):
            return False
        return True
    # With a normal build pool present, defensive/boot answers may always leave
    # the pool. Offensive situational items may do so only when they match the
    # champion archetype (e.g. Sea Halberd for physical carries, Glowing Wand
    # for mages). This keeps adaptation useful without recommending nonsense.
    if tags & {"boots", "universal", "defense"}:
        return True
    if "magic" in tags and ({"magic", "mage"} & cand_arch):
        return True
    if "physical" in tags and ({"marksman", "fighter", "assassin", "physical"} & cand_arch):
        return True
    return False




def enforce_single_boot_rule(base: list[str], situational: list[str], adaptation_scores=None) -> tuple[list[str], list[str]]:
    """Keep at most one Movement/Boot item across the final build inputs.

    A situational boot with adaptation pressure replaces the normal build boot
    instead of being appended beside it.  If several adaptive boots compete,
    the highest adaptation score wins deterministically.
    """
    base = list(dict.fromkeys(base or []))
    situational = list(dict.fromkeys(situational or []))
    scores = adaptation_scores or {}
    situational_boots = [name for name in situational if is_boot_item(name)]
    base_boots = [name for name in base if is_boot_item(name)]
    if not situational_boots and len(base_boots) <= 1:
        return base, situational

    chosen = ""
    chosen_from_situational = False
    if situational_boots:
        indexed = {name: index for index, name in enumerate(situational_boots)}
        def score(name: str) -> tuple[float, int]:
            try:
                value = float(scores.get(name, 0.0))
            except (TypeError, ValueError):
                value = 0.0
            return value, -indexed[name]
        chosen = max(situational_boots, key=score)
        chosen_from_situational = True
    elif base_boots:
        chosen = base_boots[0]

    base_out = [name for name in base if not is_boot_item(name)]
    situational_out = [name for name in situational if not is_boot_item(name)]
    if chosen:
        if chosen_from_situational:
            situational_out.insert(0, chosen)
        else:
            base_out.append(chosen)
    return base_out, situational_out


def order_build_items(base: list[str], situational: list[str], pool: list[dict], adaptation_scores) -> list[str]:
    """Return a practical purchase-priority order.

    Build Trends priority is the baseline.  Situational pressure may move an item
    earlier, but weak generic relevance should not completely erase the
    champion's normal core order.  Direct counter-items receive large scores in
    ``recommend_build`` and therefore can become first purchases when needed.
    """
    candidates = list(dict.fromkeys(list(base or []) + list(situational or [])))
    if not candidates:
        return []

    priority_by_norm: dict[str, int] = {}
    for row in pool or []:
        name = str(row.get("item_name") or "")
        if not name:
            continue
        try:
            pr = int(row.get("priority") or 999)
        except (TypeError, ValueError):
            pr = 999
        priority_by_norm[norm_item(name)] = pr

    fallback_priority = max([*priority_by_norm.values(), len(priority_by_norm), 1]) + 4
    original_index = {norm_item(name): idx for idx, name in enumerate(candidates)}

    def urgency_for(name: str) -> float:
        try:
            return max(0.0, float((adaptation_scores or {}).get(name, 0.0)))
        except (TypeError, ValueError):
            return 0.0

    def key(name: str):
        src = priority_by_norm.get(norm_item(name), fallback_priority)
        urgency = min(6.0, urgency_for(name))
        # Six priority places per urgency point is strong enough for a real
        # counter item to jump ahead, while small generic relevance stays near
        # the original Build Trends order.
        effective = float(src) - urgency * 6.0
        return (effective, src, original_index.get(norm_item(name), 999))

    return sorted(candidates, key=key)

def recommend_build(
    champion_name: str,
    enemies: list[tuple[str, str]],
    role_ru: str = "",
    snapshot: dict | None = None,
) -> dict:
    """Build from the hero's live lane core, then adapt at most the flexible slots."""
    champ = _find_champ(champion_name, snapshot)
    if not champ:
        raise ValueError("Герой не найден в локальной базе")
    enemy_objs = [(_find_champ(name, snapshot), role) for name, role in enemies]
    enemy_objs = [(e, r) for e, r in enemy_objs if e]
    cid = str(champ["id"])

    pool = _item_pool(cid, snapshot)
    arch = archetype(champ)

    role_build = None
    role_situational: list[dict] = []
    role_boots: list[dict] = []
    if role_ru:
        if snapshot is not None:
            role_build = _role_build_row(cid, role_ru, snapshot)
            role_situational = list(snapshot.get("role_situational", {}).get((cid, role_ru), []))
            role_boots = list(snapshot.get("role_boots", {}).get((cid, role_ru), []))
        else:
            role_build = db.get_role_build(cid, role_ru, "mlbb.rone")
            role_situational = db.get_role_build_situational(cid, role_ru, "mlbb.rone")
            role_boots = db.get_role_build_boots(cid, role_ru, "mlbb.rone")

    # Start with the source's lane-specific identity. Fall back to the legacy
    # item pool only when that specific role has no downloaded live build.
    base_core: list[str] = []
    base_boot = ""
    if role_build:
        base_core = [
            str(x) for x in (role_build.get("items") or [])
            if str(x).strip() and _finished_item(str(x), snapshot)
        ][:5]
        base_boot = str(role_build.get("boot_name") or "")
        if base_boot and not _finished_item(base_boot, snapshot):
            base_boot = ""

    # A measured MLBB build may intentionally contain only its three statistical
    # core items. Preserve them and fill only the flexible tail from the hero's
    # downloaded pool instead of discarding the measured core.
    fallback_boots: list[str] = []
    for p in pool:
        name = str(p.get("item_name") or "")
        cat = str(p.get("category") or "").casefold()
        if not name or not _finished_item(name, snapshot):
            continue
        if is_boot_item(name, cat):
            if name not in fallback_boots:
                fallback_boots.append(name)
        elif name not in base_core and len(base_core) < 5:
            base_core.append(name)
    base_core = base_core[:5]
    if not base_boot and fallback_boots:
        base_boot = fallback_boots[0]

    base = [*base_core]
    if base_boot:
        base.append(base_boot)

    # Compatibility set includes source role variants/situational items so a
    # legitimate lane-specific alternative is never rejected as "off-build".
    pool_names = {norm_item(x["item_name"]) for x in pool}
    for row in role_situational:
        if row.get("item_name"):
            pool_names.add(norm_item(row["item_name"]))
    for row in role_boots:
        if row.get("item_name"):
            pool_names.add(norm_item(row["item_name"]))
    pool_names.update(norm_item(x) for x in base if x)
    has_pool = bool(pool_names)

    reasons = defaultdict(list)
    reason_details = defaultdict(list)
    scores = Counter()
    threat_enemies: list[str] = []
    neutral_enemies: list[str] = []

    for enemy, _erole in enemy_objs:
        edge = _matchup_score(cid, enemy["id"], role_ru, snapshot)
        if edge < 0:
            threat_enemies.append(enemy["name"])
        else:
            neutral_enemies.append(enemy["name"])
        severity = 2.0 if edge < 0 else 1.0

        for ci in _counter_items(enemy["id"], snapshot):
            item = ci["item_name"]
            if _compatible(item, pool_names, arch, has_pool):
                scores[item] += 2.5 * severity
                reasons[item].append(f"против {enemy['name']}")
                reason_details[item].append({"kind": "direct", "enemy": enemy["name"]})

        generic_tags = _generic_threat_tags(enemy)
        candidate_names = list(dict.fromkeys(
            [str(p.get("item_name") or "") for p in pool]
            + [str(r.get("item_name") or "") for r in role_situational]
            + base
        ))
        for item in candidate_names:
            if not item or not _finished_item(item, snapshot):
                continue
            hit = generic_tags & tags_for(item)
            if not hit:
                continue
            scores[item] += 0.45 * severity * len(hit)
            if "anti_magic" in hit:
                reasons[item].append(f"магический урон/контроль от {enemy['name']}")
                reason_details[item].append({"kind": "anti_magic", "enemy": enemy["name"]})
            if "anti_physical" in hit or "anti_auto" in hit:
                reasons[item].append(f"физический урон/автоатаки {enemy['name']}")
                reason_details[item].append({"kind": "anti_physical", "enemy": enemy["name"]})
            if "anti_crit" in hit:
                reasons[item].append(f"критический урон {enemy['name']}")
                reason_details[item].append({"kind": "anti_crit", "enemy": enemy["name"]})
            if "anti_burst" in hit:
                reasons[item].append(f"взрывной урон {enemy['name']}")
                reason_details[item].append({"kind": "anti_burst", "enemy": enemy["name"]})
            if "anti_tank" in hit:
                reasons[item].append(f"прочность {enemy['name']}")
                reason_details[item].append({"kind": "anti_tank", "enemy": enemy["name"]})
            if "anti_cc" in hit:
                reasons[item].append(f"контроль {enemy['name']}")
                reason_details[item].append({"kind": "anti_cc", "enemy": enemy["name"]})

    # WRC-style identity protection: first three non-boot core items are locked.
    # Only the final two non-boot slots are adaptive by default.
    max_swaps = 2 if len(enemy_objs) < 4 else 3
    protected = set(base_core[:3])
    situational: list[str] = []
    for item, _score in scores.most_common():
        if not item or item in base or item in situational:
            continue
        if is_boot_item(item):
            continue
        if not _compatible(item, pool_names, arch, has_pool):
            continue
        situational.append(item)
        if len(situational) >= max_swaps:
            break

    final_core = list(base_core[:5])
    replaceable = [i for i in range(len(final_core) - 1, -1, -1) if final_core[i] not in protected]
    for item, index in zip(situational, replaceable):
        final_core[index] = item

    # Boots remain a dedicated slot. A role-specific alternative may replace it
    # only when it received a real counter score.
    final_boot = base_boot
    scored_boots = [
        (name, float(scores.get(name, 0.0)))
        for name in dict.fromkeys(str(r.get("item_name") or "") for r in role_boots)
        if name and is_boot_item(name) and float(scores.get(name, 0.0)) > 0
    ]
    if scored_boots:
        final_boot = max(scored_boots, key=lambda row: row[1])[0]

    ordered = [x for x in final_core if x]
    if final_boot:
        ordered.append(final_boot)

    # Final invariant: six unique finished items and never two boots.
    filtered: list[str] = []
    seen_boot = False
    for item in ordered:
        if item in filtered or not _finished_item(item, snapshot):
            continue
        if is_boot_item(item):
            if seen_boot:
                continue
            seen_boot = True
        filtered.append(item)
    ordered = filtered[:6]

    neutral_mode = not threat_enemies and not situational
    if neutral_mode:
        for item in ordered:
            reasons[item].append("нейтральный матчап: сохраняем основной билд выбранной линии")
            reason_details[item].append({"kind": "core", "enemy": ""})

    return {
        "champion": champ,
        "role": role_ru,
        "base": base,
        "situational": situational,
        "ordered": ordered,
        "reasons": {k: list(dict.fromkeys(v)) for k, v in reasons.items()},
        "reason_details": {
            k: list({(d.get("kind", ""), d.get("enemy", "")): d for d in v}.values())
            for k, v in reason_details.items()
        },
        "threat_enemies": threat_enemies,
        "neutral_enemies": neutral_enemies,
        "pool_size": len(pool),
        "source_role_build": bool(role_build),
    }

