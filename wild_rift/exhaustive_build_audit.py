from __future__ import annotations

import json
import random
from collections import Counter, defaultdict

import db
import engine
from paths import ensure_initial_data

SEED = 370120929
RANDOM_FULL_DRAFTS_PER_PAIR = 200


def legal_roles(champ):
    return [r for r in engine.CANONICAL_ROLES if engine.lane_ok(champ, r)]


def default_enemy_role(champ):
    roles = legal_roles(champ)
    return roles[0] if roles else ""


def make_full_team(snapshot, rng, exclude=frozenset(), forced=None):
    forced = list(forced or [])
    used = set(exclude)
    out = []
    for cid, role in forced:
        if cid in used:
            continue
        out.append((cid, role))
        used.add(cid)
    for role in engine.CANONICAL_ROLES:
        if len(out) >= 5:
            break
        candidates = [
            c for c in snapshot["champions"]
            if c.get("id") not in used and engine.lane_ok(c, role)
        ]
        if not candidates:
            continue
        c = rng.choice(candidates)
        out.append((c["id"], role))
        used.add(c["id"])
    # Fill any missing slots with legal champions and their first legal role.
    if len(out) < 5:
        candidates = [c for c in snapshot["champions"] if c.get("id") not in used]
        rng.shuffle(candidates)
        for c in candidates:
            roles = legal_roles(c)
            if not roles:
                continue
            out.append((c["id"], roles[0]))
            used.add(c["id"])
            if len(out) >= 5:
                break
    return out[:5]


def audit_result(result, allowed_items, champion_id, role, draft, failures):
    if result.get("fallback_used") or result.get("source") == "wrpocket.app:fallback":
        failures.append({
            "kind": "fallback",
            "champion": champion_id,
            "role": role,
            "draft": draft,
            "source": result.get("source"),
            "source_missing": result.get("source_missing"),
        })
        return

    if result.get("source_missing"):
        failures.append({
            "kind": "source_missing",
            "champion": champion_id,
            "role": role,
            "draft": draft,
        })

    ordered = list(result.get("ordered") or [])
    if len(ordered) != len(set(ordered)):
        failures.append({
            "kind": "duplicate_items",
            "champion": champion_id,
            "role": role,
            "draft": draft,
            "ordered": ordered,
        })

    if len(ordered) != 6:
        failures.append({
            "kind": "wrong_item_count",
            "champion": champion_id,
            "role": role,
            "draft": draft,
            "ordered": ordered,
        })

    foreign = [x for x in ordered if x not in allowed_items]
    if foreign:
        failures.append({
            "kind": "foreign_item",
            "champion": champion_id,
            "role": role,
            "draft": draft,
            "foreign": foreign,
            "ordered": ordered,
        })

    boots = [
        x for x in ordered
        if engine.is_boot_item(x, engine._item_category(x, None))
    ]
    if len(boots) != 1:
        failures.append({
            "kind": "boot_count",
            "champion": champion_id,
            "role": role,
            "draft": draft,
            "boots": boots,
            "ordered": ordered,
        })


def main():
    ensure_initial_data()
    db.init_db()
    snapshot = db.load_runtime_snapshot()
    rng = random.Random(SEED)

    champions = list(snapshot.get("champions") or [])
    by_id = {c["id"]: c for c in champions}
    role_builds = snapshot.get("role_builds") or {}

    static_failures = []
    allowed_by_pair = {}

    # Strongest proof for fallback: its trigger depends only on champion+role
    # source row and healthy core length, not on enemy draft.
    for (champion_id, role), row in role_builds.items():
        champ = by_id.get(champion_id)
        if not champ:
            static_failures.append({"kind":"missing_champion","champion":champion_id,"role":role})
            continue
        core, boot, situ, boots, source_row = engine._approved_role_build(
            champ, role, snapshot
        )
        if source_row is None:
            static_failures.append({"kind":"missing_source_row","champion":champion_id,"role":role})
        if len(core) < 3:
            static_failures.append({
                "kind":"short_core","champion":champion_id,"role":role,"core":core
            })

        allowed = set(core)
        if boot:
            allowed.add(boot)
        for rr in snapshot.get("role_variants", {}).get((champion_id, role), []):
            allowed.update(str(x) for x in (rr.get("items") or []) if str(x))
        for rr in snapshot.get("role_situational", {}).get((champion_id, role), []):
            if rr.get("item_name"):
                allowed.add(str(rr["item_name"]))
        for rr in snapshot.get("role_opponent_adaptations", {}).get((champion_id, role), []):
            if rr.get("item_name"):
                allowed.add(str(rr["item_name"]))
        for rr in snapshot.get("role_boots", {}).get((champion_id, role), []):
            if rr.get("item_name"):
                allowed.add(str(rr["item_name"]))
        allowed_by_pair[(champion_id, role)] = allowed

    dynamic_failures = []
    calls = 0
    changed_pairs = set()
    situational_pairs = set()
    variant_pairs = set()
    seen_builds = defaultdict(set)
    seen_variants = defaultdict(set)

    # Exhaustive one-enemy matrix: every source-backed champion+role build
    # against every other champion in every legal enemy role.
    for (champion_id, role), _row in role_builds.items():
        allowed = allowed_by_pair[(champion_id, role)]
        for enemy in champions:
            if enemy["id"] == champion_id:
                continue
            eroles = legal_roles(enemy) or [""]
            for enemy_role in eroles:
                draft = [(enemy["id"], enemy_role)]
                try:
                    result = engine.recommend_build(
                        champion_id, draft, role_ru=role, snapshot=snapshot
                    )
                except Exception as exc:
                    dynamic_failures.append({
                        "kind":"exception_single",
                        "champion":champion_id,
                        "role":role,
                        "draft":draft,
                        "error":repr(exc),
                    })
                    continue
                calls += 1
                audit_result(result, allowed, champion_id, role, draft, dynamic_failures)
                key=(champion_id, role)
                seen_builds[key].add(tuple(result.get("ordered") or []))
                if result.get("situational"):
                    situational_pairs.add(key)
                v=str(result.get("selected_variant") or "")
                if v:
                    seen_variants[key].add(v)

    # Full 5-man stress matrix. Every champion+role pair receives 200 legal
    # five-role enemy drafts; deterministic seed makes failures reproducible.
    for (champion_id, role), _row in role_builds.items():
        allowed = allowed_by_pair[(champion_id, role)]
        key=(champion_id, role)
        for _ in range(RANDOM_FULL_DRAFTS_PER_PAIR):
            draft=make_full_team(snapshot, rng, {champion_id})
            try:
                result=engine.recommend_build(
                    champion_id, draft, role_ru=role, snapshot=snapshot
                )
            except Exception as exc:
                dynamic_failures.append({
                    "kind":"exception_full",
                    "champion":champion_id,
                    "role":role,
                    "draft":draft,
                    "error":repr(exc),
                })
                continue
            calls += 1
            audit_result(result, allowed, champion_id, role, draft, dynamic_failures)
            seen_builds[key].add(tuple(result.get("ordered") or []))
            if result.get("situational"):
                situational_pairs.add(key)
            v=str(result.get("selected_variant") or "")
            if v:
                seen_variants[key].add(v)

    for key, builds in seen_builds.items():
        if len(builds) > 1:
            changed_pairs.add(key)
    for key, vals in seen_variants.items():
        if len(vals) > 1:
            variant_pairs.add(key)

    # Exhaust every exact WRC opponent adaptation row, not a sample.
    exact_total=0
    exact_triggered=0
    exact_build_changed=0
    exact_failures=[]
    for (champion_id, role), rows in (snapshot.get("role_opponent_adaptations") or {}).items():
        for rr in rows:
            exact_total += 1
            enemy_name=str(rr.get("enemy_name") or "")
            item_name=str(rr.get("item_name") or "")
            enemy=db.resolve_snapshot_champion(snapshot, enemy_name)
            if not enemy:
                exact_failures.append({
                    "kind":"enemy_unresolved","champion":champion_id,"role":role,
                    "enemy":enemy_name,"item":item_name
                })
                continue
            erole=default_enemy_role(enemy)
            baseline=make_full_team(snapshot, rng, {champion_id, enemy["id"]})
            matched=make_full_team(snapshot, rng, {champion_id}, [(enemy["id"], erole)])
            try:
                before=engine.recommend_build(champion_id, baseline, role_ru=role, snapshot=snapshot)
                after=engine.recommend_build(champion_id, matched, role_ru=role, snapshot=snapshot)
            except Exception as exc:
                exact_failures.append({
                    "kind":"exception","champion":champion_id,"role":role,
                    "enemy":enemy_name,"item":item_name,"error":repr(exc)
                })
                continue
            hits=after.get("exact_opponent_adaptations") or []
            hit=any(
                db.normalize_search(str(x.get("enemy") or "")) == db.normalize_search(enemy_name)
                and str(x.get("item") or "") == item_name
                for x in hits
            )
            if hit:
                exact_triggered += 1
            else:
                exact_failures.append({
                    "kind":"not_triggered","champion":champion_id,"role":role,
                    "enemy":enemy_name,"item":item_name,
                    "situational":after.get("situational") or [],
                    "ordered":after.get("ordered") or [],
                })
            if tuple(before.get("ordered") or []) != tuple(after.get("ordered") or []):
                exact_build_changed += 1

    summary={
        "database":{
            "champions":len(champions),
            "role_builds":len(role_builds),
            "variants":sum(len(v) for v in (snapshot.get("role_variants") or {}).values()),
            "situational_rows":sum(len(v) for v in (snapshot.get("role_situational") or {}).values()),
            "opponent_adaptation_rows":sum(len(v) for v in (snapshot.get("role_opponent_adaptations") or {}).values()),
            "boot_rows":sum(len(v) for v in (snapshot.get("role_boots") or {}).values()),
        },
        "fallback_static_audit":{
            "checked_pairs":len(role_builds),
            "failures":len(static_failures),
            "examples":static_failures[:20],
        },
        "dynamic_stress":{
            "recommend_build_calls":calls,
            "failures":len(dynamic_failures),
            "failure_kinds":dict(Counter(x["kind"] for x in dynamic_failures)),
            "examples":dynamic_failures[:20],
            "pairs_with_multiple_builds":len(changed_pairs),
            "pairs_with_situational_output":len(situational_pairs),
            "pairs_with_multiple_variants":len(variant_pairs),
        },
        "exact_opponent_adaptations":{
            "rows":exact_total,
            "triggered":exact_triggered,
            "build_changed":exact_build_changed,
            "failures":len(exact_failures),
            "examples":exact_failures[:20],
        },
    }
    print("WRCA_EXHAUSTIVE_AUDIT_BEGIN")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("WRCA_EXHAUSTIVE_AUDIT_END")

    assert not static_failures, static_failures[:3]
    assert not dynamic_failures, dynamic_failures[:3]
    assert exact_triggered == exact_total, exact_failures[:3]


if __name__ == "__main__":
    main()
