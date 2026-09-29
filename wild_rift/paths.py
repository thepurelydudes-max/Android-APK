"""Android/Flet filesystem layout for Wild Rift Counter Assistant.

The Windows build stores mutable data beside the executable. Android application
bundles are read-only, so this version stores SQLite/cache in FLET_APP_STORAGE_DATA
and seeds them from bundled Flet assets on first launch.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = Path(os.environ.get("FLET_ASSETS_DIR", str(MODULE_DIR / "assets"))).resolve()
RUNTIME_DIR = Path(os.environ.get("FLET_APP_STORAGE_DATA", str(MODULE_DIR / ".mobile_runtime"))).resolve()
APP_DIR = RUNTIME_DIR / "data"


def bundle_dir(**_kwargs) -> Path:
    # On the mobile port bundled resources live in Flet's assets directory.
    # Accept ignored compatibility kwargs used by desktop launchers.
    return ASSETS_DIR


def runtime_dir(**_kwargs) -> Path:
    return RUNTIME_DIR


def data_dir(**_kwargs) -> Path:
    return APP_DIR


def resource_path(*parts: str, bundle_root=None) -> Path:
    root = Path(bundle_root).resolve() if bundle_root is not None else ASSETS_DIR
    return root.joinpath(*parts)


def resolve_media_path(value: str, *, runtime_root=None, bundle_root=None) -> Path:
    runtime = Path(runtime_root).resolve() if runtime_root is not None else RUNTIME_DIR
    bundle = Path(bundle_root).resolve() if bundle_root is not None else ASSETS_DIR
    text = str(value or "").replace("\\", "/")
    if text.startswith("cache/") or "/cache/" in text:
        rel = "cache/" + text.split("cache/", 1)[1]
        candidate = (runtime / rel).resolve()
        if candidate.exists():
            return candidate
        return (bundle / rel).resolve()
    if text.startswith("assets/") or "/assets/" in text:
        rel = text.split("assets/", 1)[1]
        return (bundle / rel).resolve()
    path = Path(text)
    return path if path.is_absolute() else runtime / path


def portable_media_path(value: str, *, runtime_root=None, bundle_root=None) -> str:
    if not value:
        return ""
    runtime = Path(runtime_root).resolve() if runtime_root is not None else RUNTIME_DIR
    bundle = Path(bundle_root).resolve() if bundle_root is not None else ASSETS_DIR
    path = resolve_media_path(value, runtime_root=runtime, bundle_root=bundle).resolve()
    try:
        return path.relative_to(runtime).as_posix()
    except ValueError:
        pass
    try:
        return path.relative_to(bundle).as_posix()
    except ValueError:
        return str(path)


def _copy_tree_once(source: Path, target: Path) -> None:
    """Merge missing bundled seed files without overwriting newer downloaded cache."""
    target.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return
    for item in source.iterdir():
        dst = target / item.name
        if item.is_dir():
            _copy_tree_once(item, dst)
        elif not dst.exists():
            shutil.copy2(item, dst)


def _copy_seed_on_fresh_install(seed_db: Path, database: Path) -> None:
    """Install the bundled database only when writable runtime DB is absent.

    A clean Android uninstall removes app storage, so the first launch of a
    newly installed APK always starts from the exact wildrift.db bundled in that
    APK. Later launches never overwrite the writable runtime database, allowing
    the in-app updater to keep newer downloaded data.
    """
    if not seed_db.is_file():
        raise FileNotFoundError(f"Bundled database is missing: {seed_db}")
    if database.exists():
        return

    database.parent.mkdir(parents=True, exist_ok=True)
    tmp = database.with_suffix(database.suffix + ".seed-new")
    shutil.copy2(seed_db, tmp)
    tmp.replace(database)

def ensure_initial_data() -> Path:
    """Create writable Android data/cache from the immutable bundled seed.

    There is deliberately no database-quality comparison here. On a clean
    install the bundled DB is copied verbatim; after that the runtime DB is
    preserved until Android app data is removed or the user updates it in-app.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    seed_db = ASSETS_DIR / "data" / "wildrift.db"
    database = APP_DIR / "wildrift.db"

    # Android uses separate immutable assets and writable app storage. Windows
    # portable mode deliberately points both roots at the EXE folder. Avoid
    # copying a file/tree onto itself in that case.
    try:
        same_db = seed_db.resolve() == database.resolve()
    except Exception:
        same_db = False
    if not same_db:
        _copy_seed_on_fresh_install(seed_db, database)
    elif not database.is_file():
        raise FileNotFoundError(f"Portable database is missing: {database}")

    seed_cache = ASSETS_DIR / "cache"
    runtime_cache = RUNTIME_DIR / "cache"
    try:
        same_cache = seed_cache.resolve() == runtime_cache.resolve()
    except Exception:
        same_cache = False
    if not same_cache:
        _copy_tree_once(seed_cache, runtime_cache)

    (RUNTIME_DIR / "logs").mkdir(parents=True, exist_ok=True)
    return APP_DIR
