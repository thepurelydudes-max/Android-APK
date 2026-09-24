from __future__ import annotations

from collections import Counter, defaultdict
import re

import db

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
# MLBB currently has no independent tier table in its local data source, so the
# tier slot remains neutral (50) instead of inventing meta data.
FINAL_WEIGHTS = {
    "matchup": 0.60,
    "coverage": 0.20,
    "tier": 0.15,
    "winrate": 0.05,
}

TIER_SCORE = {"S+": 100.0, "S": 80.0, "A": 60.0, "B": 40.0, "C": 20.0, "D": 0.0}
TIER_ORDER = {"S+": 6, "S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}
CANONICAL_ROLES = ("EXP", "Лес", "Мид", "Голд", "Роум")

# MLBB matchup rows use a ±1.5 scale, while the Wild Rift engine uses ±3.
# Normalize both to -1..+1 before weighting so the resulting 0..100 score has
# the same meaning in both applications.
MATCHUP_ABS_MAX = 1.5
HARD_MATCHUP_THRESHOLD = MATCHUP_ABS_MAX * (2.0 / 3.0)

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
    # The MLBB database has no independent tier-list table at present.  Returning
    # an empty tier makes the shared scoring model use the neutral value (50).
    return ""


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


def recommend_picks(role_ru: str, enemies: list[tuple[str, str]], limit: int = 8, snapshot: dict | None = None) -> list[dict]:
    """Rank MLBB heroes with the same draft-first model as Wild Rift.

    Score components are normalized to 0..100 and weighted as follows:
      60% matchup strength against the whole enemy draft
      20% number of entered enemies the candidate actually counters
      15% meta tier (neutral until MLBB has an independent tier source)
       5% current role win-rate percentile

    Enemy roles are inferred from lane metadata and role-specific statistics.
    Every entered enemy remains in the calculation; inferred roles only adjust
    the weight of the likely direct lane opponent.
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
        # Accept a stats row as role evidence too: the statistics source may
        # learn a new flex role before static lane metadata is refreshed.
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

    out = []
    enemy_count = max(1, len(enemy_objs))

    for cand, tier, st in candidate_rows:
        positives: list[str] = []
        negatives: list[str] = []
        neutral: list[str] = []
        positive_strength = 0.0
        negative_strength = 0.0
        hard_counters = 0
        direct_lane_edges: list[float] = []
        weighted_edge_sum = 0.0
        total_weight = 0.0

        for enemy, inferred_role in enemy_objs:
            edge = float(_matchup_score(cand["id"], enemy["id"], role_ru, snapshot))
            edge = max(-MATCHUP_ABS_MAX, min(MATCHUP_ABS_MAX, edge))
            weight = _line_weight(role_ru, inferred_role)
            total_weight += weight
            weighted_edge_sum += (edge / MATCHUP_ABS_MAX) * weight

            if weight > 1.0:
                direct_lane_edges.append(edge)

            if edge > 0:
                positives.append(enemy["name"])
                positive_strength += edge * weight
                if edge >= HARD_MATCHUP_THRESHOLD:
                    hard_counters += 1
            elif edge < 0:
                negatives.append(enemy["name"])
                negative_strength += abs(edge) * weight
            else:
                neutral.append(enemy["name"])

        avg_edge = weighted_edge_sum / total_weight if total_weight > 0 else 0.0
        avg_edge = max(-1.0, min(1.0, avg_edge))
        matchup_score = 50.0 + 50.0 * avg_edge

        coverage_count = len(positives)
        coverage_score = 100.0 * coverage_count / enemy_count
        tier_score = TIER_SCORE.get(tier, 50.0)

        wr = None
        if st and st.get("win_rate") is not None:
            try:
                wr = float(st["win_rate"])
            except (TypeError, ValueError):
                wr = None
        winrate_score = _winrate_percentile(wr, role_win_rates)

        matchup_contribution = FINAL_WEIGHTS["matchup"] * matchup_score
        coverage_contribution = FINAL_WEIGHTS["coverage"] * coverage_score
        tier_contribution = FINAL_WEIGHTS["tier"] * tier_score
        winrate_contribution = FINAL_WEIGHTS["winrate"] * winrate_score
        score = matchup_contribution + coverage_contribution + tier_contribution + winrate_contribution

        lane_hard_loss = any(edge <= -HARD_MATCHUP_THRESHOLD for edge in direct_lane_edges)

        out.append({
            "champion": cand,
            "score": score,
            "positive": positives,
            "negative": negatives,
            "neutral": neutral,
            "positive_strength": positive_strength,
            "negative_strength": negative_strength,
            "hard_counters": hard_counters,
            "coverage_count": coverage_count,
            "coverage_total": len(enemy_objs),
            "coverage_score": coverage_score,
            "coverage_bonus": coverage_contribution,
            "tier": tier,
            "tier_score": tier_score,
            "tier_bonus": tier_contribution,
            "win_rate": wr,
            "winrate_score": winrate_score,
            "matchup_score": matchup_score,
            "matchup_contribution": matchup_contribution,
            "winrate_bonus": winrate_contribution,
            "lane_hard_loss": lane_hard_loss,
            "enemy_roles": {enemy["id"]: inferred_role for enemy, inferred_role in enemy_objs},
        })

    out.sort(
        key=lambda x: (
            not x.get("lane_hard_loss", False),
            x["score"],
            x["matchup_score"],
            x["coverage_count"],
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
    typ = str(enemy.get("damage_type", "")).casefold()
    tags = set()
    if "marksman" in roles:
        tags |= {"anti_physical", "anti_crit", "anti_auto"}
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

def recommend_build(champion_name: str, enemies: list[tuple[str, str]], snapshot: dict | None = None) -> dict:
    champ = _find_champ(champion_name, snapshot)
    if not champ:
        raise ValueError("Герой не найден в локальной базе")
    enemy_objs = [(_find_champ(name, snapshot), role) for name, role in enemies]
    enemy_objs = [(e, r) for e, r in enemy_objs if e]
    pool = _item_pool(champ["id"], snapshot)
    pool_names = {norm_item(x["item_name"]) for x in pool}
    has_pool = bool(pool)
    arch = archetype(champ)

    # Skeleton: first trend items, but avoid blindly taking 5 boots/duplicates.
    base = []
    boots = []
    for p in pool:
        name = p["item_name"]
        cat = (p.get("category") or "").casefold()
        if is_boot_item(name, cat):
            boots.append(name)
        elif name not in base:
            base.append(name)
    base = base[:5]
    if boots:
        base.append(boots[0])

    reasons = defaultdict(list)
    reason_details = defaultdict(list)
    scores = Counter()
    threat_enemies = []
    neutral_enemies = []

    for enemy, erole in enemy_objs:
        edge = _matchup_score(champ["id"], enemy["id"], "", snapshot)
        if edge < 0:
            threat_enemies.append(enemy["name"])
        else:
            neutral_enemies.append(enemy["name"])
        direct = _counter_items(enemy["id"], snapshot)
        severity = 2.0 if edge < 0 else 1.0
        for ci in direct:
            item = ci["item_name"]
            if _compatible(item, pool_names, arch, has_pool):
                scores[item] += 2.5 * severity
                reasons[item].append(f"против {enemy['name']}")
                reason_details[item].append({"kind": "direct", "enemy": enemy["name"]})

        generic_tags = _generic_threat_tags(enemy)
        for p in pool:
            item = p["item_name"]
            itags = tags_for(item)
            hit = generic_tags & itags
            if hit:
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

    situational = []
    for item, score in scores.most_common():
        if item in situational:
            continue
        # Don't flood the build: 2 adaptations is usually enough; 3 only if there are multiple threats.
        situational.append(item)
        if len(situational) >= (3 if len(enemy_objs) >= 4 else 2):
            break

    # If no matchup pressure was detected, preserve champion identity and maximize normal pool.
    neutral_mode = not threat_enemies and not situational
    if neutral_mode:
        situational = base[:2]
        for i in situational:
            reasons[i].append("нейтральный матчап: оставляем сильный предмет из обычного пула чемпиона")
            reason_details[i].append({"kind": "core", "enemy": ""})

    base, situational = enforce_single_boot_rule(base, situational, scores)
    ordered = order_build_items(base, situational, pool, scores)

    # Defensive invariant for every caller/UI surface: a final recommendation
    # can never expose two pairs of boots even if a future source adds one.
    seen_boot = False
    filtered_ordered = []
    for item in ordered:
        if is_boot_item(item):
            if seen_boot:
                continue
            seen_boot = True
        filtered_ordered.append(item)
    ordered = filtered_ordered

    return {
        "champion": champ,
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
    }
