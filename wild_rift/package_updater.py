"""GitHub package updater for WLCA.

The Android app never scrapes Wild Rift data sources directly. A complete,
validated data package is assembled in GitHub and published as a release asset.
The app downloads that one immutable ZIP, verifies hashes + SQLite integrity,
then swaps it into the writable runtime only after every check succeeds.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from paths import RUNTIME_DIR

PROTOCOL_VERSION = 1
LATEST_MANIFEST_URL = (
    "https://raw.githubusercontent.com/"
    "thepurelydudes-max/Android-APK/main/wlca_data/latest.json"
)
TRUSTED_RELEASE_PREFIX = (
    "https://github.com/thepurelydudes-max/Android-APK/releases/download/"
)
UPDATE_DIR = RUNTIME_DIR / "github-update"
PART_PATH = UPDATE_DIR / "WLCA-data.zip.part"
PART_META_PATH = UPDATE_DIR / "WLCA-data.part.json"
STAGE_DIR = UPDATE_DIR / "stage"
BACKUP_DIR = UPDATE_DIR / "rollback"
RECOVERY_MARKER = UPDATE_DIR / "promotion.json"
INSTALLED_MANIFEST = RUNTIME_DIR / "data" / "package_manifest.json"
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 3000


class PackageUpdateError(RuntimeError):
    pass


def _app_version() -> str:
    path = Path(__file__).with_name("VERSION.txt")
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


def _version_tuple(value: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in str(value or "").strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out or [0])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_sha256(folder: Path) -> str:
    """Hash cache contents by relative path + file bytes, independent of mtimes."""
    h = hashlib.sha256()
    if not folder.is_dir():
        return ""
    for path in sorted(x for x in folder.rglob("*") if x.is_file()):
        rel = path.relative_to(folder).as_posix().encode("utf-8")
        h.update(len(rel).to_bytes(4, "big"))
        h.update(rel)
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PackageUpdateError(f"Некорректный manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageUpdateError("Некорректный manifest: ожидается JSON object.")
    return value


def _fetch_latest(session: requests.Session) -> dict:
    url = f"{LATEST_MANIFEST_URL}?nocache={int(time.time())}"
    response = session.get(
        url,
        timeout=(15, 45),
        headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
    )
    response.raise_for_status()
    try:
        manifest = response.json()
    except Exception as exc:
        raise PackageUpdateError(f"GitHub вернул некорректный latest.json: {exc}") from exc

    if not isinstance(manifest, dict):
        raise PackageUpdateError("GitHub latest.json имеет неверный формат.")
    if int(manifest.get("protocol_version") or 0) != PROTOCOL_VERSION:
        raise PackageUpdateError(
            "Версия протокола пакета несовместима с этим APK. "
            "Потребуется обновить само приложение."
        )
    package_version = str(manifest.get("package_version") or "").strip()
    package_sha = str(manifest.get("sha256") or "").strip().lower()
    manifest_sha = str(manifest.get("manifest_sha256") or "").strip().lower()
    download_url = str(manifest.get("download_url") or "").strip()
    size_bytes = int(manifest.get("size_bytes") or 0)
    min_app = str(manifest.get("min_app_version") or "").strip()

    if not package_version:
        raise PackageUpdateError("latest.json не содержит package_version.")
    if not re_full_sha(package_sha) or not re_full_sha(manifest_sha):
        raise PackageUpdateError("latest.json содержит некорректный SHA-256.")
    if not download_url.startswith(TRUSTED_RELEASE_PREFIX):
        raise PackageUpdateError("latest.json содержит недоверенный адрес пакета.")
    parsed = urlparse(download_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise PackageUpdateError("Пакет должен скачиваться только с GitHub Releases.")
    if size_bytes <= 0 or size_bytes > MAX_PACKAGE_BYTES:
        raise PackageUpdateError(f"Некорректный размер пакета: {size_bytes}.")
    if min_app and _version_tuple(_app_version()) < _version_tuple(min_app):
        raise PackageUpdateError(
            f"Пакет требует WLCA {min_app} или новее; установлено {_app_version()}."
        )
    return manifest


def re_full_sha(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _database_counts(database: Path) -> dict[str, int]:
    if not database.is_file():
        raise PackageUpdateError("В пакете отсутствует data/wildrift.db.")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as con:
            quick = con.execute("PRAGMA quick_check").fetchone()
            if not quick or str(quick[0]).casefold() != "ok":
                raise PackageUpdateError("SQLite quick_check не пройден.")
            names = {
                str(row[0])
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = (
                "champions", "stats", "champion_tiers", "matchups", "items",
                "item_pools", "role_builds", "role_build_variants",
                "counter_items", "build_page_cache", "matchup_page_cache",
            )
            missing = [name for name in required if name not in names]
            if missing:
                raise PackageUpdateError(
                    "В базе отсутствуют таблицы: " + ", ".join(missing)
                )
            counts = {
                table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in required
            }
            counts["role_build_champions"] = int(
                con.execute("SELECT COUNT(DISTINCT champion_id) FROM role_builds").fetchone()[0]
            )
            counts["build_cache_champions"] = int(
                con.execute("SELECT COUNT(DISTINCT champion_id) FROM build_page_cache").fetchone()[0]
            )
            counts["matchup_cache_champions"] = int(
                con.execute("SELECT COUNT(DISTINCT champion_id) FROM matchup_page_cache").fetchone()[0]
            )
            return counts
    except PackageUpdateError:
        raise
    except Exception as exc:
        raise PackageUpdateError(f"Не удалось проверить SQLite: {exc}") from exc


def _count_pngs(folder: Path) -> int:
    return sum(1 for path in folder.glob("*.png") if path.is_file())


def _validate_payload(root: Path, remote: dict) -> dict:
    internal_path = root / "manifest.json"
    data_path = root / "data" / "wildrift.db"
    cache_path = root / "cache"
    if not internal_path.is_file():
        raise PackageUpdateError("В ZIP отсутствует manifest.json.")
    if not cache_path.is_dir():
        raise PackageUpdateError("В ZIP отсутствует cache/.")

    if _sha256(internal_path) != str(remote["manifest_sha256"]).lower():
        raise PackageUpdateError("SHA-256 внутреннего manifest.json не совпадает.")

    internal = _load_json(internal_path)
    for key in ("protocol_version", "package_version", "min_app_version", "patch"):
        if str(internal.get(key) or "") != str(remote.get(key) or ""):
            raise PackageUpdateError(f"latest.json и пакет расходятся по полю {key}.")

    db_hash = str(internal.get("database_sha256") or "").lower()
    if not re_full_sha(db_hash) or _sha256(data_path) != db_hash:
        raise PackageUpdateError("Контрольная сумма wildrift.db не совпадает.")

    cache_hash = str(internal.get("cache_sha256") or "").lower()
    if not re_full_sha(cache_hash) or _tree_sha256(cache_path) != cache_hash:
        raise PackageUpdateError("Контрольная сумма cache/ не совпадает.")

    actual = _database_counts(data_path)
    expected = dict(internal.get("counts") or {})
    if not expected:
        raise PackageUpdateError("В manifest отсутствуют контрольные counts.")

    for key, wanted in expected.items():
        if key in {"champion_icons", "item_icons"}:
            continue
        if key not in actual:
            continue
        if int(actual[key]) != int(wanted):
            raise PackageUpdateError(
                f"Проверка {key} не пройдена: {actual[key]} вместо {wanted}."
            )

    champions = int(actual.get("champions") or 0)
    if champions <= 0:
        raise PackageUpdateError("Пакет содержит пустой список чемпионов.")
    if int(actual.get("build_cache_champions") or 0) != champions:
        raise PackageUpdateError(
            "WRC build cache неполный: "
            f"{actual.get('build_cache_champions', 0)}/{champions}."
        )
    if int(actual.get("matchup_cache_champions") or 0) != champions:
        raise PackageUpdateError(
            "WRC matchup cache неполный: "
            f"{actual.get('matchup_cache_champions', 0)}/{champions}."
        )
    if int(actual.get("role_build_champions") or 0) != champions:
        raise PackageUpdateError(
            "WRC role builds покрывают не всех чемпионов: "
            f"{actual.get('role_build_champions', 0)}/{champions}."
        )

    champ_icons = _count_pngs(cache_path / "champions")
    item_icons = _count_pngs(cache_path / "items")
    if champ_icons != int(expected.get("champion_icons") or 0):
        raise PackageUpdateError(
            f"Портреты чемпионов: {champ_icons} вместо {expected.get('champion_icons')}."
        )
    if item_icons != int(expected.get("item_icons") or 0):
        raise PackageUpdateError(
            f"Иконки предметов: {item_icons} вместо {expected.get('item_icons')}."
        )

    # Store the exact package manifest beside the installed DB after promotion.
    target_manifest = root / "data" / "package_manifest.json"
    shutil.copy2(internal_path, target_manifest)
    return internal


def _safe_extract(package: Path, target: Path) -> None:
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(package, "r") as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_ARCHIVE_FILES:
            raise PackageUpdateError("ZIP содержит недопустимое число файлов.")
        total_size = sum(max(0, int(info.file_size)) for info in infos)
        if total_size <= 0 or total_size > MAX_EXTRACTED_BYTES:
            raise PackageUpdateError("Распакованный пакет имеет недопустимый размер.")

        for info in infos:
            name = str(info.filename or "").replace("\\", "/")
            path = Path(name)
            if (
                not name
                or path.is_absolute()
                or ".." in path.parts
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise PackageUpdateError(f"Недопустимый путь в ZIP: {name}")
            allowed = (
                name == "manifest.json"
                or name == "data/wildrift.db"
                or name.startswith("cache/")
            )
            if not allowed:
                raise PackageUpdateError(f"Неожиданный файл в пакете: {name}")

        archive.extractall(target)


def _download_package(
    session: requests.Session,
    remote: dict,
    progress: Callable[[str], None],
) -> Path:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    expected = int(remote["size_bytes"])
    url = str(remote["download_url"])

    # A resumable fragment belongs to exactly one immutable release asset.
    # Never append bytes from a newer package to an older .part file.
    resume_identity = {
        "package_version": str(remote.get("package_version") or ""),
        "sha256": str(remote.get("sha256") or "").lower(),
        "size_bytes": expected,
        "download_url": url,
    }
    old_identity = {}
    if PART_META_PATH.is_file():
        try:
            old_identity = json.loads(PART_META_PATH.read_text(encoding="utf-8"))
        except Exception:
            old_identity = {}
    if old_identity != resume_identity:
        PART_PATH.unlink(missing_ok=True)
        PART_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        PART_META_PATH.write_text(
            json.dumps(resume_identity, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    current = PART_PATH.stat().st_size if PART_PATH.is_file() else 0
    if current > expected:
        PART_PATH.unlink(missing_ok=True)
        current = 0

    def render(done: int) -> None:
        done_mb = done / (1024 * 1024)
        total_mb = expected / (1024 * 1024)
        progress(f"2/4 GitHub: скачиваю пакет {done_mb:.1f}/{total_mb:.1f} МБ")

    if current == expected:
        render(current)
        return PART_PATH

    headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    if current:
        headers["Range"] = f"bytes={current}-"

    response = session.get(
        url,
        stream=True,
        timeout=(20, 90),
        headers=headers,
        allow_redirects=True,
    )

    if current and response.status_code == 416 and current == expected:
        response.close()
        return PART_PATH

    append = current > 0 and response.status_code == 206
    if current and response.status_code == 200:
        # GitHub/CDN ignored Range. Restart safely rather than append duplicates.
        current = 0
        append = False
    if response.status_code not in (200, 206):
        response.raise_for_status()

    mode = "ab" if append else "wb"
    done = current if append else 0
    last_report = 0.0
    with PART_PATH.open(mode) as fh:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            fh.write(chunk)
            done += len(chunk)
            if done > expected:
                raise PackageUpdateError("GitHub прислал больше данных, чем указано в manifest.")
            now = time.monotonic()
            if now - last_report >= 0.25:
                render(done)
                last_report = now
        fh.flush()
        os.fsync(fh.fileno())
    response.close()
    render(done)

    if done != expected:
        raise PackageUpdateError(
            f"Загрузка оборвалась: получено {done} из {expected} байт. "
            "Неполный файл сохранён и будет докачан при следующей попытке."
        )
    return PART_PATH


def _marker_payload() -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "created_at": int(time.time()),
        "db": str(RUNTIME_DIR / "data" / "wildrift.db"),
        "manifest": str(INSTALLED_MANIFEST),
        "cache": str(RUNTIME_DIR / "cache"),
        "backup": str(BACKUP_DIR),
    }


def recover_interrupted_update() -> bool:
    """Restore the previous working set if Android killed us during promotion."""
    if not RECOVERY_MARKER.is_file():
        return False

    live_db = RUNTIME_DIR / "data" / "wildrift.db"
    live_manifest = INSTALLED_MANIFEST
    live_cache = RUNTIME_DIR / "cache"
    backup_db = BACKUP_DIR / "data" / "wildrift.db"
    backup_manifest = BACKUP_DIR / "data" / "package_manifest.json"
    backup_cache = BACKUP_DIR / "cache"

    try:
        if backup_db.is_file():
            live_db.parent.mkdir(parents=True, exist_ok=True)
            live_db.unlink(missing_ok=True)
            backup_db.replace(live_db)
        if backup_manifest.is_file():
            live_manifest.unlink(missing_ok=True)
            backup_manifest.replace(live_manifest)
        elif live_manifest.exists():
            live_manifest.unlink(missing_ok=True)
        if backup_cache.is_dir():
            if live_cache.exists():
                shutil.rmtree(live_cache, ignore_errors=True)
            backup_cache.rename(live_cache)
    finally:
        shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        RECOVERY_MARKER.unlink(missing_ok=True)
    return True


def _promote(stage: Path) -> None:
    recover_interrupted_update()
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)
    (BACKUP_DIR / "data").mkdir(parents=True, exist_ok=True)

    live_db = RUNTIME_DIR / "data" / "wildrift.db"
    live_manifest = INSTALLED_MANIFEST
    live_cache = RUNTIME_DIR / "cache"
    stage_db = stage / "data" / "wildrift.db"
    stage_manifest = stage / "data" / "package_manifest.json"
    stage_cache = stage / "cache"
    backup_db = BACKUP_DIR / "data" / "wildrift.db"
    backup_manifest = BACKUP_DIR / "data" / "package_manifest.json"
    backup_cache = BACKUP_DIR / "cache"

    RECOVERY_MARKER.write_text(
        json.dumps(_marker_payload(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    try:
        live_db.parent.mkdir(parents=True, exist_ok=True)
        if live_db.exists():
            live_db.replace(backup_db)
        if live_manifest.exists():
            live_manifest.replace(backup_manifest)
        if live_cache.exists():
            live_cache.rename(backup_cache)

        stage_db.replace(live_db)
        stage_manifest.replace(live_manifest)
        stage_cache.rename(live_cache)

        # Final verification on the actual installed path before discarding rollback.
        _database_counts(live_db)
    except Exception:
        recover_interrupted_update()
        raise
    else:
        shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        RECOVERY_MARKER.unlink(missing_ok=True)


def _installed_is_current(remote: dict) -> bool:
    if not INSTALLED_MANIFEST.is_file():
        return False
    try:
        installed = _load_json(INSTALLED_MANIFEST)
        if str(installed.get("package_version") or "") != str(remote.get("package_version") or ""):
            return False
        db_path = RUNTIME_DIR / "data" / "wildrift.db"
        cache_path = RUNTIME_DIR / "cache"
        actual = _database_counts(db_path)
        if _sha256(db_path) != str(installed.get("database_sha256") or "").lower():
            return False
        if _tree_sha256(cache_path) != str(installed.get("cache_sha256") or "").lower():
            return False
        expected = dict(installed.get("counts") or {})
        for key, value in expected.items():
            if key in actual and int(actual[key]) != int(value):
                return False
        if _count_pngs(RUNTIME_DIR / "cache" / "champions") != int(expected.get("champion_icons") or 0):
            return False
        if _count_pngs(RUNTIME_DIR / "cache" / "items") != int(expected.get("item_icons") or 0):
            return False
        return True
    except Exception:
        return False


def update_all(
    progress: Callable[[str], None] | None = None,
    lang: str = "ru",
    cancel_check=None,
) -> dict:
    """Install the newest complete data package from GitHub Releases."""
    del lang, cancel_check
    emit = progress or (lambda _message: None)
    recover_interrupted_update()
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": f"WLCA/{_app_version()} GitHubPackageUpdater",
        "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
    })

    emit("1/4 GitHub: проверяю доступную версию базы…")
    remote = _fetch_latest(session)

    if _installed_is_current(remote):
        emit("4/4 База уже актуальна.")
        return {
            "patch": str(remote.get("patch") or ""),
            "package_version": str(remote.get("package_version") or ""),
            "already_current": True,
            "downloaded": False,
            "errors": [],
            "source": "GitHub Releases",
        }

    package = _download_package(session, remote, emit)

    emit("3/4 Проверяю SHA-256, manifest и целостность SQLite…")
    if _sha256(package) != str(remote["sha256"]).lower():
        package.unlink(missing_ok=True)
        raise PackageUpdateError(
            "SHA-256 скачанного пакета не совпадает. Файл удалён; рабочая база не изменена."
        )

    _safe_extract(package, STAGE_DIR)
    internal = _validate_payload(STAGE_DIR, remote)

    emit("4/4 Устанавливаю полностью проверенный пакет…")
    _promote(STAGE_DIR)

    # A successfully installed package no longer needs the resumable download.
    PART_PATH.unlink(missing_ok=True)
    PART_META_PATH.unlink(missing_ok=True)
    shutil.rmtree(STAGE_DIR, ignore_errors=True)

    return {
        "patch": str(internal.get("patch") or remote.get("patch") or ""),
        "package_version": str(internal.get("package_version") or ""),
        "already_current": False,
        "downloaded": True,
        "errors": [],
        "source": "GitHub Releases",
        "counts": dict(internal.get("counts") or {}),
    }
