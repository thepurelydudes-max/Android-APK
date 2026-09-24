"""Android/Flet filesystem layout for Wild Rift Counter Assistant.

The Windows build stores mutable data beside the executable. Android application
bundles are read-only, so this version stores SQLite/cache in FLET_APP_STORAGE_DATA
and seeds them from bundled Flet assets on first launch.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = Path(os.environ.get("FLET_ASSETS_DIR", str(MODULE_DIR / "assets"))).resolve()
RUNTIME_DIR = Path(os.environ.get("FLET_APP_STORAGE_DATA", str(MODULE_DIR / ".mobile_runtime"))).resolve()
APP_DIR = RUNTIME_DIR / "data"


def bundle_dir(*, module_file=None) -> Path:
    # On the mobile port bundled resources live in Flet's assets directory.
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


def _patch_key(value: str) -> tuple[int, ...]:
    """Comparable numeric patch key: 7.10 sorts after 7.9; suffixes are ignored."""
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(x) for x in parts[:3]) if parts else (0,)


def _seed_db_quality(path: Path) -> tuple[tuple[int, ...], int, int]:
    """Return (patch, WildRiftCore matchup rows, cached champion pages)."""
    if not path.is_file():
        return (0,), 0, 0
    try:
        with sqlite3.connect(path) as con:
            patch_row = con.execute("SELECT value FROM meta WHERE key='patch_version'").fetchone()
            patch = _patch_key(patch_row[0] if patch_row else "")
            try:
                matchups = int(con.execute(
                    "SELECT COUNT(*) FROM matchups WHERE source='wildriftcore.com'"
                ).fetchone()[0])
            except sqlite3.Error:
                matchups = 0
            try:
                pages = int(con.execute(
                    "SELECT COUNT(*) FROM matchup_page_cache WHERE source='wildriftcore.com'"
                ).fetchone()[0])
            except sqlite3.Error:
                pages = 0
            return patch, matchups, pages
    except sqlite3.Error:
        return (0,), 0, 0


def _copy_seed_if_better(seed_db: Path, database: Path) -> None:
    """Seed/upgrade Android DB without overwriting a newer downloaded database.

    This matters when an APK is installed over an older release: Android preserves
    app storage, so merely bundling a new SQLite file would otherwise leave the old
    runtime DB in place forever.
    """
    if not seed_db.is_file():
        raise FileNotFoundError(f"Bundled database is missing: {seed_db}")
    if not database.exists():
        shutil.copy2(seed_db, database)
        return

    seed_quality = _seed_db_quality(seed_db)
    runtime_quality = _seed_db_quality(database)
    seed_patch, seed_matchups, seed_pages = seed_quality
    run_patch, run_matchups, run_pages = runtime_quality

    should_upgrade = False
    if seed_patch > run_patch:
        should_upgrade = True
    elif seed_patch == run_patch:
        # On the same patch prefer the richer WRC matrix/cache bundled with the
        # release, but never replace a runtime DB that has already downloaded more.
        should_upgrade = (seed_matchups, seed_pages) > (run_matchups, run_pages)

    if should_upgrade:
        # Preserve lightweight user-facing settings stored in meta.
        preserved: dict[str, str] = {}
        try:
            with sqlite3.connect(database) as con:
                for key in ("lang", "layout_mode"):
                    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
                    if row:
                        preserved[key] = str(row[0])
        except sqlite3.Error:
            preserved = {}

        tmp = database.with_suffix(database.suffix + ".seed-new")
        shutil.copy2(seed_db, tmp)
        tmp.replace(database)
        if preserved:
            try:
                with sqlite3.connect(database) as con:
                    for key, value in preserved.items():
                        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, value))
            except sqlite3.Error:
                pass


def ensure_initial_data() -> Path:
    """Create/upgrade writable Android data/cache from the immutable bundled seed."""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    seed_db = ASSETS_DIR / "data" / "wildrift.db"
    database = APP_DIR / "wildrift.db"
    _copy_seed_if_better(seed_db, database)
    _copy_tree_once(ASSETS_DIR / "cache", RUNTIME_DIR / "cache")
    (RUNTIME_DIR / "logs").mkdir(parents=True, exist_ok=True)
    return APP_DIR
