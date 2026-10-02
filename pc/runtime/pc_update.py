"""Windows-safe deferred installation for already verified GitHub data packages."""
from __future__ import annotations
import hashlib, json, os, shutil, sqlite3, time, uuid
from pathlib import Path

class PendingUpdateError(RuntimeError): pass

def _root(runtime): return Path(runtime) / "github-update"
def _marker(runtime): return _root(runtime) / "pc-pending.json"
def _rollback(runtime): return _root(runtime) / "pc-rollback"

def _sha256(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda:f.read(1<<20),b""): h.update(chunk)
    return h.hexdigest()

def _tree_sha256(folder):
    folder=Path(folder); h=hashlib.sha256()
    if not folder.is_dir(): return ""
    files=[p for p in folder.rglob("*") if p.is_file()]
    files.sort(key=lambda p:p.relative_to(folder).as_posix())
    for p in files:
        rel=p.relative_to(folder).as_posix().encode("utf-8"); h.update(len(rel).to_bytes(4,"big")); h.update(rel)
        with p.open("rb") as f:
            for chunk in iter(lambda:f.read(1<<20),b""): h.update(chunk)
    return h.hexdigest()

def _retry(fn, timeout=15.0):
    end=time.monotonic()+timeout; delay=.08
    while True:
        try: return fn()
        except OSError:
            if time.monotonic()>=end: raise
            time.sleep(delay); delay=min(.8,delay*1.55)

def _best_rmtree(path):
    try: shutil.rmtree(Path(path),ignore_errors=True)
    except Exception: pass

def _copy_file_atomic(src,dst):
    src,dst=Path(src),Path(dst); dst.parent.mkdir(parents=True,exist_ok=True)
    tmp=dst.with_name(dst.name+f".pc-new-{uuid.uuid4().hex[:8]}"); shutil.copy2(src,tmp)
    try:
        with tmp.open("rb") as f: os.fsync(f.fileno())
    except OSError: pass
    try: _retry(lambda:os.replace(tmp,dst))
    finally: tmp.unlink(missing_ok=True)

def _manifest(root):
    root=Path(root)
    for p in (root/"manifest.json",root/"data"/"package_manifest.json"):
        if p.is_file():
            v=json.loads(p.read_text(encoding="utf-8"))
            if isinstance(v,dict): return v
    raise PendingUpdateError("В подготовленном обновлении отсутствует manifest.json")

def _verify_db(path):
    path=Path(path)
    if not path.is_file(): raise PendingUpdateError(f"Отсутствует база: {path.name}")
    con=sqlite3.connect(f"file:{path}?mode=ro",uri=True)
    try:
        row=con.execute("PRAGMA quick_check").fetchone()
        if not row or str(row[0]).casefold()!="ok": raise PendingUpdateError("SQLite quick_check не пройден")
    finally: con.close()

def verify_payload(root,db_filename):
    root=Path(root); m=_manifest(root); db=root/"data"/db_filename; cache=root/"cache"; _verify_db(db)
    if not cache.is_dir(): raise PendingUpdateError("В обновлении отсутствует cache/")
    dh=str(m.get("database_sha256") or "").lower(); ch=str(m.get("cache_sha256") or "").lower()
    if len(dh)==64 and _sha256(db)!=dh: raise PendingUpdateError("SHA-256 базы не совпадает")
    if len(ch)==64 and _tree_sha256(cache)!=ch: raise PendingUpdateError("SHA-256 cache не совпадает")
    return m

def pending_info(runtime):
    p=_marker(runtime)
    if not p.is_file(): return {}
    try:
        v=json.loads(p.read_text(encoding="utf-8")); return v if isinstance(v,dict) else {}
    except Exception: return {}

def pending_dir(runtime,info=None):
    info=info if isinstance(info,dict) else pending_info(runtime); name=str(info.get("pending_dir") or "")
    if name.startswith("pc-pending-") and Path(name).name==name: return _root(runtime)/name
    return _root(runtime)/"pc-pending"

def pending_update_ready(runtime,db_filename=None):
    p=pending_dir(runtime)
    return _marker(runtime).is_file() and p.is_dir() and (not db_filename or (p/"data"/db_filename).is_file())

def _cleanup(runtime,keep=None):
    r=_root(runtime)
    if not r.is_dir(): return
    for p in r.iterdir():
        if not p.is_dir() or not (p.name in {"pc-pending","pc-pending.new"} or p.name.startswith("pc-pending-")): continue
        try:
            if keep and p.resolve()==Path(keep).resolve(): continue
        except Exception: pass
        _best_rmtree(p)

def stage_pending_update(stage,runtime,db_filename,product):
    stage,runtime=Path(stage),Path(runtime); m=verify_payload(stage,db_filename); r=_root(runtime); r.mkdir(parents=True,exist_ok=True)
    pending=r/f"pc-pending-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}"; shutil.copytree(stage,pending); verify_payload(pending,db_filename)
    payload={"product":product,"db_filename":db_filename,"created_at":int(time.time()),"package_version":str(m.get("package_version") or ""),"patch":str(m.get("patch") or ""),"state":"verified-waiting-for-restart","pending_dir":pending.name}
    tmp=r/f"pc-pending.json.new-{uuid.uuid4().hex[:8]}"; tmp.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    try: _retry(lambda:os.replace(tmp,_marker(runtime)))
    except Exception: tmp.unlink(missing_ok=True); _best_rmtree(pending); raise
    _cleanup(runtime,keep=pending); return payload

def _backup_live(runtime,db_filename):
    runtime=Path(runtime); rb=_rollback(runtime); _best_rmtree(rb); (rb/"data").mkdir(parents=True,exist_ok=True)
    db=runtime/"data"/db_filename
    if db.is_file(): shutil.copy2(db,rb/"data"/db_filename)
    pm=runtime/"data"/"package_manifest.json"
    if pm.is_file(): shutil.copy2(pm,rb/"data"/"package_manifest.json")
    cache=runtime/"cache"
    if cache.is_dir(): shutil.copytree(cache,rb/"cache")
    return rb

def _restore(runtime,db_filename):
    runtime=Path(runtime); rb=_rollback(runtime)
    if (rb/"data"/db_filename).is_file(): _copy_file_atomic(rb/"data"/db_filename,runtime/"data"/db_filename)
    if (rb/"data"/"package_manifest.json").is_file(): _copy_file_atomic(rb/"data"/"package_manifest.json",runtime/"data"/"package_manifest.json")
    if (rb/"cache").is_dir(): _best_rmtree(runtime/"cache"); shutil.copytree(rb/"cache",runtime/"cache")

def apply_pending_update(runtime,db_filename):
    runtime=Path(runtime)
    if not pending_update_ready(runtime,db_filename): return False
    pending=pending_dir(runtime); verify_payload(pending,db_filename); rb=_backup_live(runtime,db_filename)
    try:
        _copy_file_atomic(pending/"data"/db_filename,runtime/"data"/db_filename)
        pm=pending/"data"/"package_manifest.json"
        if not pm.is_file() and (pending/"manifest.json").is_file(): pm=pending/"manifest.json"
        if pm.is_file(): _copy_file_atomic(pm,runtime/"data"/"package_manifest.json")
        new_cache=runtime/f"cache.pc-new-{uuid.uuid4().hex[:8]}"; _best_rmtree(new_cache); shutil.copytree(pending/"cache",new_cache)
        live=runtime/"cache"; old=rb/"cache-live"; _best_rmtree(old)
        if live.exists(): _retry(lambda:live.rename(old))
        try: _retry(lambda:new_cache.rename(live))
        except Exception:
            if old.exists() and not live.exists(): _retry(lambda:old.rename(live))
            raise
        verify_payload(runtime,db_filename)
    except Exception: _restore(runtime,db_filename); raise
    _marker(runtime).unlink(missing_ok=True); _best_rmtree(pending); _best_rmtree(rb); _cleanup(runtime); return True

def write_apply_error(runtime,exc):
    try:
        p=Path(runtime)/"logs"; p.mkdir(parents=True,exist_ok=True); (p/"pc-update-apply-error.log").write_text(str(exc),encoding="utf-8")
    except Exception: pass
