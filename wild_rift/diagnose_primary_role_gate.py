from __future__ import annotations
import json
import db, engine
from paths import ensure_initial_data

ROLES=list(engine.CANONICAL_ROLES)
DRAFT=[("Irelia","Барон"),("Kaisa","ADC"),("Hecarim","Лес"),("Soraka","Саппорт"),("Ambessa","Мид")]

def usable(c, role, s):
    return engine.lane_ok(c, role) and engine._has_usable_role_build(c, role, s)

def role_pick_rate(c, role, s):
    lane=engine.ROLE_TO_STAT.get(role,"")
    st=engine._stat(c.get("id",""),lane,"all",s) if lane else None
    if not st or st.get("pick_rate") is None:
        return 0.0
    try:return float(st.get("pick_rate") or 0.0)
    except:return 0.0

def primary_role(c,s):
    opts=[]
    for idx,role in enumerate(ROLES):
        if not usable(c,role,s): continue
        pr=role_pick_rate(c,role,s)
        tier=engine.TIER_ORDER.get(engine._tier(c.get("id",""),role,s),0)
        # pick rate dominates, then role tier, then canonical stable order
        opts.append((pr,tier,-idx,role))
    return max(opts)[3] if opts else ""

def main():
    ensure_initial_data(); db.init_db(); s=db.load_runtime_snapshot()
    counts={r:0 for r in ROLES}
    mult=[]
    for c in s["champions"]:
        roles=[r for r in ROLES if usable(c,r,s)]
        p=primary_role(c,s)
        if p: counts[p]+=1
        if len(roles)>1:
            mult.append({
                "id":c["id"],
                "name":c.get("name"),
                "roles":[{"role":r,"pick_rate":role_pick_rate(c,r,s),"tier":engine._tier(c["id"],r,s)} for r in roles],
                "primary":p,
                "class":c.get("roles",[]),
            })
    print("PRIMARY_COUNTS",json.dumps(counts,ensure_ascii=False))
    print("MULTI_ROLE_COUNT",len(mult))
    print("MULTI_ROLE_SAMPLE")
    print(json.dumps(mult,ensure_ascii=False,indent=2))

    # Compare current vs strict-primary candidate lists on user's draft.
    for role in ROLES:
        rows=engine.recommend_picks(role,DRAFT,limit=30,snapshot=s)
        strict=[r for r in rows if primary_role(r["champion"],s)==role]
        print("\nROLE",role)
        print("CURRENT_TOP",[(r["champion"]["id"],r["score"],r["positive"],r["negative"]) for r in rows[:15]])
        print("STRICT_TOP",[(r["champion"]["id"],r["score"],r["positive"],r["negative"]) for r in strict[:15]])

if __name__=="__main__":
    main()
