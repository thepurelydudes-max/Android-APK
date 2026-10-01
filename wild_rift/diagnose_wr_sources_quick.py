from __future__ import annotations
import json, os, tempfile, traceback
from pathlib import Path
from bs4 import BeautifulSoup

runtime=Path(tempfile.mkdtemp(prefix="wrca-quick-"))
os.environ["FLET_APP_STORAGE_DATA"]=str(runtime)

import paths
paths.ensure_initial_data()
import sources, updater, db

def say(k,v):
    print(f"{k}: {v}", flush=True)

net=sources.Net()
try:
    en=sources.fetch_champions_locale(net,"en_US")
    ru=sources.fetch_champions_locale(net,"ru_RU")
    champs=updater.merge_champion_locales(en,ru)
    try:
        extra=sources.fetch_wildriftmeta_champion_roster(net)
        champs=updater.merge_champion_roster_supplement(champs,extra)
        say("wildriftmeta_roster",len(extra))
    except Exception as e:
        say("wildriftmeta_error",repr(e))
    say("champions_en",len(en))
    say("champions_ru",len(ru))
    say("champions_merged",len(champs))
    resolve=updater.build_resolver(champs)

    patch=sources.fetch_current_patch_info(net)
    say("patch",patch)
    date,stats=sources.fetch_stats(net)
    say("stats_date",date)
    say("stats_rows",len(stats))
    say("stats_resolved",sum(1 for s in stats if resolve(s["champion_id"])))

    profiles=sources._wildriftcore_profile_links(net,resolve,print)
    say("wrc_profiles",len(profiles))
    say("wrc_profile_sample",[x[0] for x in profiles[:5]])

    tiers=sources.parse_wildriftcore_tiers(net,resolve,print)
    by_role={}
    for cid,role,tier in tiers:
        by_role[role]=by_role.get(role,0)+1
    say("wrc_tiers",json.dumps(by_role,ensure_ascii=False))

    # Parse one representative WRC build and counter page end-to-end.
    aatrox=dict(profiles).get("Aatrox")
    if aatrox:
        build_text,transport=sources._wildriftcore_build_text(net,aatrox+"/builds/",print)
        payload=sources.parse_wildriftcore_build_page(build_text,"Aatrox",db.get_item_names())
        say("aatrox_build_transport",transport)
        say("aatrox_build_roles",[r.get("role") for r in payload.get("builds",[])])
        say("aatrox_variants",len(payload.get("variants",[])))
        ctext=sources._jina_reader_get(net,aatrox+"/counters/",print).text if getattr(net,"_wildriftcore_reader_only",False) else sources._wildriftcore_get(net,aatrox+"/counters/",print,sources.WR_CORE_HTML_HEADERS).text
        rows=sources._parse_wildriftcore_counter_page(ctext,"Aatrox",resolve)
        say("aatrox_matchup_rows",len(rows))
        say("aatrox_matchup_roles",sorted({r[2] for r in rows}))

    wp=net.get(sources.WR_POCKET_CHAMPS).text
    soup=BeautifulSoup(wp,"html.parser")
    wp_urls={a.get("href") for a in soup.find_all("a",href=True) if "/en/champions/" in str(a.get("href") or "")}
    say("wrpocket_profile_links",len(wp_urls))

    ds=sources.fetch_wrpocket_item_dataset(net,None,{})
    say("wrpocket_items_status",ds.status_code)
    say("wrpocket_item_rows",len(ds.rows or []))

    wc=sources._wildriftcounter_get(net,sources.WR_COUNTER_CHAMPS,print).text
    soup=BeautifulSoup(wc,"html.parser")
    wc_urls={a.get("href") for a in soup.find_all("a",href=True) if "/champions/" in str(a.get("href") or "") and str(a.get("href") or "").rstrip("/")!=sources.WR_COUNTER_CHAMPS.rstrip("/")}
    say("wildriftcounter_profile_links",len(wc_urls))

except Exception as e:
    print("QUICK_PROBE_EXCEPTION:",type(e).__name__,str(e),flush=True)
    traceback.print_exc()
    raise
