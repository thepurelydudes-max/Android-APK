from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from paths import RUNTIME_DIR

BASE_DIR = RUNTIME_DIR
CACHE_DIR = BASE_DIR / "cache"
CHAMPION_DIR = CACHE_DIR / "champions"
ITEM_DIR = CACHE_DIR / "items"
BRAND_DIR = CACHE_DIR / "brand"

RIOT_WILD_RIFT_LOGO_PNG = "https://www.riotgames.com/darkroom/800/17c02e33af4f199c041f3eab79e6b6e0%3A897aae57e8334f4b39a698a06ad0383c/wildrift-logo-rgb2c-gold-1.png"
RIOT_WILD_RIFT_LOGOS_ZIP = (
    "https://www.riotgames.com/darkroom/original/"
    "e0d8faf8ae33b14f587cde3b864669d7%3A5cffc64a75cec51a6f13bec08bd2be31/"
    "lol-wild-rift-logos.zip"
)


def ensure_cache_dirs() -> None:
    for folder in (CHAMPION_DIR, ITEM_DIR, BRAND_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def safe_name(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9._-]+", "_", value or "").strip("._")
    return text or "asset"


def _valid_image(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size <= 0:
            return False
        with Image.open(path) as im:
            im.verify()
        return True
    except Exception:
        return False


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _header(headers, name: str, default: str = "") -> str:
    if not headers:
        return default
    for key, value in headers.items():
        if str(key).casefold() == name.casefold():
            return str(value)
    return default


def _header_int(headers, name: str, default: int = 0) -> int:
    try:
        return int(_header(headers, name, str(default)) or default)
    except (TypeError, ValueError):
        return int(default or 0)


@dataclass(frozen=True)
class SyncResult:
    path: str
    status: str
    downloaded: bool
    source_url: str
    etag: str
    last_modified: str
    content_length: int
    sha256: str
    checked_patch: str
    last_checked: str


def _result_from_record(target: Path, record: dict, *, status: str, downloaded: bool, checked_patch: str | None = None, last_checked: str | None = None) -> SyncResult:
    return SyncResult(
        path=str(target) if target.exists() else str(record.get("local_path") or ""),
        status=status,
        downloaded=downloaded,
        source_url=str(record.get("source_url") or ""),
        etag=str(record.get("etag") or ""),
        last_modified=str(record.get("last_modified") or ""),
        content_length=int(record.get("content_length") or 0),
        sha256=str(record.get("sha256") or ""),
        checked_patch=str(checked_patch if checked_patch is not None else record.get("checked_patch") or ""),
        last_checked=str(last_checked if last_checked is not None else record.get("last_checked") or ""),
    )


def sync_cached_image(net, url: str, target_path: str | Path, record: dict | None = None, *, current_patch: str = "", previous_patch: str = "") -> SyncResult:
    """Synchronize one cached image without risking the last known-good file.

    Network policy:
    - same URL + validators -> conditional GET (304 keeps local bytes);
    - same URL + no validators + same checked patch -> no request;
    - URL or patch changed -> fetch and hash-compare;
    - invalid/error response -> keep the existing valid image untouched.
    """
    target = Path(target_path).with_suffix(".png")
    target.parent.mkdir(parents=True, exist_ok=True)
    record = dict(record or {})
    existing_valid = _valid_image(target)
    if existing_valid and not record.get("sha256"):
        record["sha256"] = file_sha256(target)
    if existing_valid and not record.get("content_length"):
        record["content_length"] = target.stat().st_size

    old_url = str(record.get("source_url") or "")
    old_etag = str(record.get("etag") or "")
    old_modified = str(record.get("last_modified") or "")
    checked_patch = str(record.get("checked_patch") or "")
    now = _now_iso()

    if not url:
        if existing_valid:
            return _result_from_record(target, record, status="cached", downloaded=False, last_checked=now)
        return SyncResult("", "missing_url", False, old_url, old_etag, old_modified, int(record.get("content_length") or 0), str(record.get("sha256") or ""), checked_patch, now)

    same_url = bool(old_url) and old_url == url
    has_validators = bool(old_etag or old_modified)
    same_patch = bool(current_patch) and checked_patch == current_patch

    # A migrated old cache with no manifest validators should not be re-downloaded
    # repeatedly during the same patch.
    if existing_valid and same_url and not has_validators and same_patch:
        record["source_url"] = url
        return _result_from_record(target, record, status="cached", downloaded=False, checked_patch=current_patch, last_checked=now)

    headers = {}
    if existing_valid and same_url:
        if old_etag:
            headers["If-None-Match"] = old_etag
        if old_modified:
            headers["If-Modified-Since"] = old_modified

    try:
        try:
            response = net.get(url, headers=headers or None, allow_not_modified=True)
        except TypeError:
            # Compatibility with simple/test network adapters that only accept URL.
            response = net.get(url)
    except Exception:
        if existing_valid:
            return _result_from_record(target, record, status="stale_kept", downloaded=False, last_checked=now)
        return SyncResult("", "failed", False, old_url, old_etag, old_modified, int(record.get("content_length") or 0), str(record.get("sha256") or ""), checked_patch, now)

    status_code = int(getattr(response, "status_code", 200) or 200)
    resp_headers = getattr(response, "headers", {}) or {}
    if status_code == 304 and existing_valid:
        updated = dict(record)
        updated["source_url"] = url
        updated["etag"] = _header(resp_headers, "ETag", old_etag)
        updated["last_modified"] = _header(resp_headers, "Last-Modified", old_modified)
        updated["checked_patch"] = current_patch or checked_patch
        updated["last_checked"] = now
        return _result_from_record(target, updated, status="not_modified", downloaded=False, checked_patch=updated["checked_patch"], last_checked=now)

    content = getattr(response, "content", b"") or b""
    if status_code < 200 or status_code >= 300 or not content:
        if existing_valid:
            return _result_from_record(target, record, status="stale_kept", downloaded=False, last_checked=now)
        return SyncResult("", "failed", False, old_url, old_etag, old_modified, int(record.get("content_length") or 0), str(record.get("sha256") or ""), checked_patch, now)

    # Fast path: some CDNs omit validators but return byte-identical content.
    # Compare response bytes with the existing file before any PNG re-encoding so
    # an unchanged asset keeps its original bytes and mtime.
    old_hash = str(record.get("sha256") or "") if existing_valid else ""
    raw_hash = hashlib.sha256(content).hexdigest()
    if existing_valid and old_hash and raw_hash == old_hash:
        return SyncResult(
            path=str(target), status="verified_same", downloaded=True, source_url=url,
            etag=_header(resp_headers, "ETag", old_etag if same_url else ""),
            last_modified=_header(resp_headers, "Last-Modified", old_modified if same_url else ""),
            content_length=_header_int(resp_headers, "Content-Length", len(content)),
            sha256=old_hash, checked_patch=current_patch or checked_patch or previous_patch, last_checked=now,
        )

    tmp = target.with_name(target.name + ".tmp")
    try:
        with Image.open(BytesIO(content)) as im:
            im.load()
            if im.width <= 0 or im.height <= 0:
                raise ValueError("empty image")
            im.convert("RGBA").save(tmp, format="PNG", optimize=True)
        if not _valid_image(tmp):
            raise ValueError("invalid normalized image")
        new_hash = file_sha256(tmp)
        old_hash = str(record.get("sha256") or "") if existing_valid else ""
        if existing_valid and old_hash and new_hash == old_hash:
            tmp.unlink(missing_ok=True)
            status = "verified_same"
        else:
            tmp.replace(target)
            status = "updated"
        return SyncResult(
            path=str(target),
            status=status,
            downloaded=True,
            source_url=url,
            etag=_header(resp_headers, "ETag", old_etag if same_url else ""),
            last_modified=_header(resp_headers, "Last-Modified", old_modified if same_url else ""),
            content_length=_header_int(resp_headers, "Content-Length", len(content)),
            sha256=new_hash,
            checked_patch=current_patch or checked_patch or previous_patch,
            last_checked=now,
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        if existing_valid:
            return _result_from_record(target, record, status="stale_kept", downloaded=False, last_checked=now)
        return SyncResult("", "failed", False, old_url, old_etag, old_modified, int(record.get("content_length") or 0), str(record.get("sha256") or ""), checked_patch, now)


def cache_image(net, url: str, target_path: str | Path) -> str:
    """Backward-compatible one-shot image cache used by older code/tests."""
    target = Path(target_path).with_suffix(".png")
    if _valid_image(target):
        return str(target)
    result = sync_cached_image(net, url, target, None)
    return result.path if result.status not in {"failed", "missing_url"} else ""


def cache_brand_logo(net) -> str:
    ensure_cache_dirs()
    target = BRAND_DIR / "wild_rift_logo.png"
    if _valid_image(target):
        return str(target)
    try:
        response = net.get(RIOT_WILD_RIFT_LOGO_PNG)
        content = getattr(response, "content", b"")
        if content:
            try:
                with Image.open(BytesIO(content)) as im:
                    im.load(); im.convert("RGBA").save(target, format="PNG", optimize=True)
                if _valid_image(target):
                    return str(target)
            except Exception:
                # Keep compatibility with Riot/test endpoints that return a PNG
                # payload whose metadata Pillow cannot fully decode yet.
                if content.startswith(b"\x89PNG\r\n\x1a\n"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                    return str(target)
    except Exception:
        pass
    try:
        response = net.get(RIOT_WILD_RIFT_LOGOS_ZIP)
        content = getattr(response, "content", b"")
        if not content:
            return ""
        with zipfile.ZipFile(BytesIO(content)) as zf:
            candidates = [n for n in zf.namelist() if n.lower().endswith(".png")]
            if not candidates:
                return ""
            preferred = sorted(candidates, key=lambda n: ("white" not in n.casefold(), "wild" not in n.casefold(), len(n)))[0]
            raw = zf.read(preferred)
            with Image.open(BytesIO(raw)) as im:
                im.load(); im.convert("RGBA").save(target, format="PNG", optimize=True)
        return str(target) if _valid_image(target) else ""
    except Exception:
        return ""
