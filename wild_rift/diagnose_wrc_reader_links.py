from __future__ import annotations
import re
import sources
net=sources.Net()
r=sources._jina_reader_get(net,sources.WR_CORE_BUILDS,print)
text=r.text or ""
print("LEN",len(text),flush=True)
lines=[line for line in text.splitlines() if "champion" in line.casefold() or "build" in line.casefold()]
print("MATCHING_LINES",len(lines),flush=True)
for line in lines[:120]:
    print(repr(line),flush=True)
patterns=[
 r"https?://(?:www\\.)?wildriftcore\\.com/en/champions/([^/?#)\\s]+)/builds/?",
 r"/en/champions/([^/?#)\\s]+)/builds/?",
 r"https?://(?:www\\.)?wildriftcore\\.com/en/champions/([^/?#)\\s]+)/?(?:[?#)\\s])",
]
for p in patterns:
    vals=re.findall(p,text,flags=re.I)
    print("PATTERN",p,"COUNT",len(vals),"SAMPLE",vals[:10],flush=True)
