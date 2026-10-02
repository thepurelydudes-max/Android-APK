from __future__ import annotations
import argparse,hashlib,json,shutil,sqlite3,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; HERE=Path(__file__).resolve().parent
CFG={
"wrca":dict(src="wild_rift",latest="wrca_data/latest.json",product="WRCA",title="Wild Rift Counter Assistant",exe="WildRiftCounterAssistant",db="wildrift.db",flet="1.0.0",icon_png="wildrift_icon.png",icon_ico="wildrift_icon.ico",role="Мид",enemy="Ahri",extra=["beautifulsoup4>=4.12.0"],proxy=False),
"mlca":dict(src="mlbb",latest="mlca_data/latest.json",product="MLCA",title="Mobile Legends Counter Assistant",exe="MobileLegendsCounterAssistant",db="mobilelegends.db",flet="1.0.1",icon_png="mobilelegends_icon.png",icon_ico="mobilelegends_icon.ico",role="Лес",enemy="Miya",extra=[],proxy=True)}
def sha(path):
 h=hashlib.sha256()
 with Path(path).open("rb") as f:
  for c in iter(lambda:f.read(1<<20),b""): h.update(c)
 return h.hexdigest()
def dl(url,path):
 req=urllib.request.Request(url,headers={"User-Agent":"FARLINER-PC-Builder/1","Accept-Encoding":"identity"}); path.parent.mkdir(parents=True,exist_ok=True)
 with urllib.request.urlopen(req,timeout=180) as a,path.open("wb") as b: shutil.copyfileobj(a,b,1<<20)
def render(name,cfg,version):
 s=(HERE/"runtime"/name).read_text(encoding="utf-8"); repl={"@@EXE@@":cfg["exe"],"@@DB@@":cfg["db"],"@@PRODUCT@@":cfg["product"],"@@TITLE@@":cfg["title"],"@@VERSION@@":version,"@@ICON_PNG@@":cfg["icon_png"],"@@ICON_ICO@@":cfg["icon_ico"],"@@ROLE@@":cfg["role"],"@@ENEMY@@":cfg["enemy"]}
 if name=="package_updater.py.tmpl": repl["@@PROXY_CODE@@"]=proxy_code() if cfg["proxy"] else "def update_all(*a,**k): return _apk.update_all(*a,**k)"
 for a,b in repl.items(): s=s.replace(a,b)
 return s
def proxy_code(): return '''_REAL_SESSION=_apk.requests.Session
_NET=(_apk.requests.exceptions.ProxyError,_apk.requests.exceptions.ConnectionError,_apk.requests.exceptions.Timeout,_apk.requests.exceptions.SSLError)
class _FallbackSession(_REAL_SESSION):
 def request(self,method,url,**kwargs):
  try:return super().request(method,url,**kwargs)
  except _NET as first:
   import urllib.request
   trust=self.trust_env; proxies=dict(self.proxies); last=first
   try:
    variants=[]; system=dict(urllib.request.getproxies() or {})
    if system:variants.append(system)
    variants.append({})
    for p in variants:
     try:self.trust_env=False;self.proxies.clear();self.proxies.update(p);return super().request(method,url,**kwargs)
     except _NET as e:last=e
    raise last
   finally:self.trust_env=trust;self.proxies.clear();self.proxies.update(proxies)
def update_all(*a,**k):
 old=_apk.requests.Session;_apk.requests.Session=_FallbackSession
 try:return _apk.update_all(*a,**k)
 finally:_apk.requests.Session=old
'''
def data(cfg,target):
 latest=json.loads((ROOT/cfg["latest"]).read_text(encoding="utf-8")); url=str(latest["download_url"]); trusted="https://github.com/thepurelydudes-max/Android-APK/releases/download/"
 if not url.startswith(trusted): raise RuntimeError("untrusted package URL")
 z=target/".data.zip"; dl(url,z)
 if sha(z)!=str(latest["sha256"]).lower(): raise RuntimeError("package SHA mismatch")
 st=target/".stage"
 with zipfile.ZipFile(z) as a:
  bad=a.testzip()
  if bad: raise RuntimeError("package CRC failed: "+bad)
  a.extractall(st)
 db=st/"data"/cfg["db"]; con=sqlite3.connect(f"file:{db}?mode=ro",uri=True)
 try:
  if con.execute("PRAGMA quick_check").fetchone()[0]!="ok": raise RuntimeError("SQLite quick_check failed")
 finally:con.close()
 shutil.copytree(st/"data",target/"data",dirs_exist_ok=True);shutil.copytree(st/"cache",target/"cache",dirs_exist_ok=True);z.unlink();shutil.rmtree(st);return latest
def build(product,out):
 cfg=CFG[product];src=ROOT/cfg["src"];version=(src/"VERSION.txt").read_text().strip();target=out/f'{cfg["exe"]}_v{version}_PC_Builder';shutil.rmtree(target,ignore_errors=True);target.mkdir(parents=True)
 for p in src.glob("*.py"):
  if not p.name.startswith("test_") and p.name!="package_updater.py":shutil.copy2(p,target/p.name)
 shutil.copy2(src/"package_updater.py",target/"package_updater_apk.py");shutil.copy2(src/"VERSION.txt",target/"VERSION.txt")
 if (src/"assets").is_dir():shutil.copytree(src/"assets",target/"assets",dirs_exist_ok=True,ignore=shutil.ignore_patterns("data","cache"))
 latest=data(cfg,target);shutil.copy2(HERE/"runtime"/"pc_update.py",target/"pc_update.py");shutil.copy2(HERE/"runtime"/"build_exe.bat",target/"build_exe.bat")
 for name,dest in (("paths.py.tmpl","paths.py"),("package_updater.py.tmpl","package_updater.py"),("launcher.py.tmpl","launcher.py"),("build_release.py.tmpl","build_release.py"),("test_pc_update.py.tmpl","tests/test_pc_update.py")):
  p=target/dest;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(render(name,cfg,version),encoding="utf-8",newline="\n")
 req=["requests>=2.32.0","Pillow>=10.4.0",f'flet[desktop]=={cfg["flet"]}',*cfg["extra"]];(target/"requirements.txt").write_text("\n".join(req)+"\n");(target/"requirements-build.txt").write_text("-r requirements.txt\nNuitka==4.2.2\nzstandard==0.25.0\n")
 (target/"README_PORTABLE_RU.md").write_text(f'# {cfg["product"]} {version} Windows Portable\n\nРаспакуйте ZIP полностью и запускайте `{cfg["exe"]}.exe`.\n',encoding="utf-8")
 meta={"product":cfg["product"],"version":version,"source":cfg["src"],"data_package":latest.get("package_version"),"patch":latest.get("patch"),"windows_deferred_update":True,"proxy_fallback":cfg["proxy"]};(target/"PC_BUILD_METADATA.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8");return target
def zipit(folder):
 z=folder.with_suffix(".zip");z.unlink(missing_ok=True)
 with zipfile.ZipFile(z,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as a:
  for p in sorted(folder.rglob("*")):
   if p.is_file():a.write(p,(Path(folder.name)/p.relative_to(folder)).as_posix())
 return z
def main():
 ap=argparse.ArgumentParser();ap.add_argument("--product",choices=CFG,required=True);ap.add_argument("--output",default="_pc");a=ap.parse_args();folder=build(a.product,Path(a.output).resolve());z=zipit(folder);print(json.dumps({"builder_dir":str(folder),"builder_zip":str(z),"version":(folder/"VERSION.txt").read_text().strip()}));return 0
if __name__=="__main__":raise SystemExit(main())
