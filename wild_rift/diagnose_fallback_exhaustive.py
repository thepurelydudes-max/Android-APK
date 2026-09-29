from __future__ import annotations
import json
import db, engine
from paths import ensure_initial_data

ROLE_ORDER = list(engine.CANONICAL_ROLES)

def main():
    ensure_initial_data()
    db.init_db()
    s = db.load_runtime_snapshot()

    legal = []
    fallback_pairs = []
    source_pairs = []
    for champ in s["champions"]:
        for role in ROLE_ORDER:
            if not engine.lane_ok(champ, role):
                continue
            legal.append((champ["id"], role))
            r = engine.recommend_build(champ["id"], [], role_ru=role, snapshot=s)
            if r.get("fallback_used") or r.get("source") == "wrpocket.app:fallback":
                fallback_pairs.append((champ["id"], role))
            else:
                source_pairs.append((champ["id"], role))

    # Exhaust every one-enemy legal role state against every requested role.
    fallback_visible = []
    candidate_seen = {}
    one_enemy_cases = 0
    for enemy in s["champions"]:
        for enemy_role in ROLE_ORDER:
            if not engine.lane_ok(enemy, enemy_role):
                continue
            enemies=[(enemy["id"], enemy_role)]
            for role in ROLE_ORDER:
                recs = engine.recommend_picks(role, enemies, limit=8, snapshot=s)
                one_enemy_cases += 1
                for rank, rec in enumerate(recs, 1):
                    cid=rec["champion"]["id"]
                    key=(cid, role)
                    candidate_seen[key]=candidate_seen.get(key,0)+1
                    if key in fallback_pairs:
                        fallback_visible.append({
                            "enemy": enemy["id"], "enemy_role": enemy_role,
                            "requested_role": role, "rank": rank,
                            "candidate": cid, "score": rec["score"],
                        })

    # Exhaust all exact WRC opponent adaptations (not a sample).
    exact_rows=[]
    for (cid, role), rows in s.get("role_opponent_adaptations", {}).items():
        for row in rows:
            exact_rows.append((cid, role, row))

    exact_fail=[]
    for cid, role, row in exact_rows:
        enemy_name=str(row.get("enemy_name") or "")
        item=str(row.get("item_name") or "")
        enemy=db.resolve_snapshot_champion(s, enemy_name)
        if not enemy:
            exact_fail.append({"champion":cid,"role":role,"enemy":enemy_name,"item":item,"reason":"enemy_unresolved"})
            continue
        # Enemy role label is optional; recommend_build infers it.
        res=engine.recommend_build(cid, [(enemy["id"], "")], role_ru=role, snapshot=s)
        hits=res.get("exact_opponent_adaptations") or []
        matched=any(
            db.normalize_search(str(h.get("enemy") or "")) == db.normalize_search(enemy_name)
            and str(h.get("item") or "") == item
            for h in hits
        )
        if not matched:
            exact_fail.append({
                "champion":cid,"role":role,"enemy":enemy_name,"item":item,
                "ordered":res.get("ordered"),"situational":res.get("situational"),
                "source":res.get("source"),"fallback":res.get("fallback_used",False)
            })

    # Database-level proof of fallback condition.
    unhealthy=[]
    for cid, role in legal:
        row=s.get("role_builds",{}).get((cid,role))
        core=[]
        if row:
            core=[str(x) for x in (row.get("items") or [])]
            core=engine._finished_only(core,s)
        if row is None or len(core)<3:
            unhealthy.append({
                "champion":cid,"role":role,
                "row_missing":row is None,
                "finished_core_len":len(core)
            })

    report={
        "champions":len(s["champions"]),
        "legal_champion_role_pairs":len(legal),
        "source_build_pairs":len(source_pairs),
        "fallback_pairs":fallback_pairs,
        "unhealthy_pairs":unhealthy,
        "one_enemy_recommendation_cases":one_enemy_cases,
        "fallback_pairs_visible_in_top8_count":len(fallback_visible),
        "fallback_visible_examples":fallback_visible[:50],
        "exact_opponent_rules_total":len(exact_rows),
        "exact_opponent_rules_failed":len(exact_fail),
        "exact_fail_examples":exact_fail[:20],
    }
    print("WRCA_EXHAUSTIVE_FALLBACK_REPORT_BEGIN")
    print(json.dumps(report,ensure_ascii=False,indent=2))
    print("WRCA_EXHAUSTIVE_FALLBACK_REPORT_END")
    if exact_fail:
        raise AssertionError(f"exact opponent adaptation failures: {len(exact_fail)}")

if __name__=="__main__":
    main()
