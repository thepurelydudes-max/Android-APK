from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import textwrap
import zipfile
from pathlib import Path

VERSION = "2.1.2"
WINDOWS_VERSION = "2.1.2.0"
SOURCE_COMMIT = "63837d7c0bd1a258c7984746a35256de0e273556"
APK_RUN_ID = "36661885778"
APK_ARTIFACT_ID = "11074887438"
APK_ARTIFACT_NAME = "MLCA-2.1.2-APK"
PROJECT_DIRNAME = "MobileLegendsCounterAssistant_v2.1.2_PC_Builder_Free"

CORE_FILES = (
    "db.py",
    "engine.py",
    "draft_matrix_engine.py",
    "data_contract.py",
    "sources.py",
    "updater.py",
    "media_cache.py",
    "localization.py",
    "adaptive_descriptions.py",
)

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def extract_apk_app(artifact_dir: Path, work_dir: Path) -> Path:
    apks = sorted(artifact_dir.rglob("*.apk"))
    if len(apks) != 1:
        raise RuntimeError(f"Expected exactly one APK in {artifact_dir}, found: {apks}")
    apk = apks[0]
    out = work_dir / "apk_app"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with zipfile.ZipFile(apk) as zf:
        app_zip_bytes = zf.read("assets/app.zip")
    nested = work_dir / "app.zip"
    nested.write_bytes(app_zip_bytes)
    with zipfile.ZipFile(nested) as zf:
        zf.extractall(out)
    version = (out / "VERSION.txt").read_text(encoding="utf-8").strip()
    if version != VERSION:
        raise RuntimeError(f"APK VERSION.txt mismatch: {version!r}")
    return out

def desktop_app(android_main: str) -> str:
    source = android_main
    old_import = "from paths import RUNTIME_DIR, ensure_initial_data, resolve_media_path"
    new_import = "from paths import RUNTIME_DIR, ensure_initial_data, resolve_media_path, resource_path"
    if old_import not in source:
        raise RuntimeError("Could not find paths import in Android main.py")
    source = source.replace(old_import, new_import, 1)

    anchor = "        self.page.scroll = ft.ScrollMode.AUTO\n"
    shell = """        # Desktop shell only: the application logic/UI below is the same MLCA 2.1.2\n        # payload as the APK, hosted in a phone-like Windows Flet window.\n        try:\n            if getattr(self.page, "window", None) is not None:\n                self.page.window.width = 450\n                self.page.window.height = 800\n                self.page.window.min_width = 430\n                self.page.window.min_height = 720\n                self.page.window.icon = str(resource_path("assets", "mobilelegends_icon.ico"))\n        except Exception:\n            pass\n"""
    if anchor not in source:
        raise RuntimeError("Could not find configure_page anchor")
    source = source.replace(anchor, anchor + shell, 1)

    marker = "\nasync def main(page: ft.Page):\n"
    pos = source.rfind(marker)
    if pos < 0:
        raise RuntimeError("Could not find Android main() tail")
    source = source[:pos] + '''

async def main(page: ft.Page, initial_lang: str = "ru"):
    """Desktop entry hosting the exact MLCA 2.1.2 Flet UI."""
    MobileAssistant(page, initial_lang=initial_lang)


def run(initial_lang: str = "ru") -> None:
    """Run the APK-equivalent UI using Flet Desktop."""
    assets_dir = resource_path("assets")

    async def _desktop_main(page: ft.Page):
        await main(page, initial_lang=initial_lang)

    ft.run(_desktop_main, assets_dir=str(assets_dir))


if __name__ == "__main__":
    run()
'''
    return source

PATHS_PY = r'''"""Portable Windows filesystem layout for MLCA PC Builder."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import sys
from pathlib import Path


def _nuitka_containing_dir() -> Path | None:
    try:
        compiled = __compiled__  # type: ignore[name-defined]
    except NameError:
        return None
    value = getattr(compiled, "containing_dir", None)
    return Path(value).resolve() if value else None


def _is_compiled_runtime() -> bool:
    return _nuitka_containing_dir() is not None or bool(getattr(sys, "frozen", False))


def bundle_dir(*, module_file: str | Path | None = None) -> Path:
    return Path(module_file or __file__).resolve().parent


def runtime_dir(
    *,
    compiled: bool | None = None,
    argv0: str | Path | None = None,
    containing_dir: str | Path | None = None,
    module_file: str | Path | None = None,
) -> Path:
    if containing_dir is not None:
        return Path(containing_dir).resolve()
    detected = _nuitka_containing_dir()
    if detected is not None:
        return detected
    if compiled is None:
        compiled = _is_compiled_runtime()
    if compiled:
        return Path(argv0 or sys.argv[0]).resolve().parent
    return bundle_dir(module_file=module_file)


def data_dir(**kwargs) -> Path:
    return runtime_dir(**kwargs) / "data"


def resource_path(*parts: str, bundle_root: str | Path | None = None) -> Path:
    root = Path(bundle_root).resolve() if bundle_root is not None else bundle_dir()
    return root.joinpath(*parts)


def resolve_media_path(
    value: str,
    *,
    runtime_root: str | Path | None = None,
    bundle_root: str | Path | None = None,
) -> Path:
    runtime = Path(runtime_root).resolve() if runtime_root is not None else runtime_dir()
    bundle = Path(bundle_root).resolve() if bundle_root is not None else bundle_dir()
    text = str(value or "").replace("\\", "/")

    if text.startswith("cache/") or "/cache/" in text:
        rel = "cache/" + text.split("cache/", 1)[1]
        external = (runtime / rel).resolve()
        if external.exists():
            return external
        embedded = (bundle / "assets" / rel).resolve()
        if embedded.exists():
            return embedded
        return external

    if text.startswith("assets/") or "/assets/" in text:
        rel = text.split("assets/", 1)[1]
        return (bundle / "assets" / rel).resolve()

    path = Path(text)
    return path if path.is_absolute() else runtime / path


def portable_media_path(
    value: str,
    *,
    runtime_root: str | Path | None = None,
    bundle_root: str | Path | None = None,
) -> str:
    if not value:
        return ""
    runtime = Path(runtime_root).resolve() if runtime_root is not None else runtime_dir()
    bundle = Path(bundle_root).resolve() if bundle_root is not None else bundle_dir()
    path = resolve_media_path(value, runtime_root=runtime, bundle_root=bundle).resolve()
    try:
        return path.relative_to(runtime).as_posix()
    except ValueError:
        pass
    try:
        return path.relative_to(bundle).as_posix()
    except ValueError:
        return str(path)


def ensure_initial_data() -> Path:
    root = runtime_dir()
    target = root / "data"
    target.mkdir(parents=True, exist_ok=True)
    database = target / "mobilelegends.db"
    legacy = root / "mobilelegends.db"
    if not database.exists() and legacy.is_file():
        temporary = database.with_suffix(".migrating")
        try:
            with closing(sqlite3.connect(legacy)) as source, closing(sqlite3.connect(temporary)) as dest:
                source.backup(dest)
            temporary.replace(database)
        finally:
            temporary.unlink(missing_ok=True)
    if not database.is_file():
        raise FileNotFoundError(
            "Не найдена data/mobilelegends.db. Распакуйте всю папку программы из ZIP."
        )
    for name in ("cache/champions", "cache/items", "cache/brand", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return target


RUNTIME_DIR = runtime_dir()
APP_DIR = data_dir()
'''

LAUNCHER_PY = r'''"""Compiled/source entry point for MLCA 2.1.2 PC Portable Free."""
import ctypes
import json
import sys
import traceback

from paths import ensure_initial_data, runtime_dir


def prepare_windows_app_identity() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "FARLINER.MobileLegendsCounterAssistant"
        )
        return result == 0
    except Exception:
        return False


def main() -> int:
    root = runtime_dir()
    sys.dont_write_bytecode = True
    try:
        prepare_windows_app_identity()
        ensure_initial_data()

        if "--self-test" in sys.argv:
            from diagnostics import check_installation
            result = check_installation(root)
            (root / "logs").mkdir(parents=True, exist_ok=True)
            (root / "logs/self-test.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(result, ensure_ascii=False))
            return 0

        import db
        db.init_db()
        legacy_lang = db.get_meta("ui_lang", "ru") or "ru"
        initial_lang = db.get_meta("lang", legacy_lang) or "ru"
        initial_lang = "ru" if initial_lang.lower().startswith("ru") else "en"

        import app
        app.run(initial_lang=initial_lang)
        return 0
    except Exception as error:
        details = traceback.format_exc()
        try:
            (root / "logs").mkdir(parents=True, exist_ok=True)
            (root / "logs/startup-error.log").write_text(details, encoding="utf-8")
        except OSError:
            pass
        if "--self-test" not in sys.argv:
            try:
                from tkinter import Tk, messagebox
                window = Tk()
                window.withdraw()
                messagebox.showerror(
                    "Mobile Legends Counter Assistant",
                    f"Не удалось запустить программу:\\n{error}\\n\\n"
                    "Распакуйте всю папку ZIP в доступную для записи папку.\\n"
                    "Подробности: logs/startup-error.log",
                )
                window.destroy()
            except Exception:
                pass
        if sys.stderr:
            print(details, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

DIAGNOSTICS_PY = r'''"""Offline integrity/parity checks for MLCA PC Builder and compiled release."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3

from PIL import Image

from paths import bundle_dir, resolve_media_path, runtime_dir

VERSION = "2.1.2"
REQUIRED_ASSETS = (
    "mobilelegends_logo.png",
    "mobilelegends_icon.png",
    "mobilelegends_icon.ico",
    "roles/gold.png",
    "roles/exp.png",
    "roles/jungle.png",
    "roles/mid.png",
    "roles/roam.png",
)


def check_installation(root: Path | None = None, *, asset_root: Path | None = None) -> dict:
    root = Path(root or runtime_dir()).resolve()
    asset_root = Path(asset_root or bundle_dir()).resolve()
    errors: list[str] = []

    for name in REQUIRED_ASSETS:
        if not (asset_root / "assets" / name).is_file():
            errors.append("assets/" + name)

    version_file = root / "VERSION.txt"
    if version_file.is_file() and version_file.read_text(encoding="utf-8").strip() != VERSION:
        errors.append("VERSION.txt mismatch")

    database = root / "data/mobilelegends.db"
    if not database.is_file():
        raise ValueError("Missing data/mobilelegends.db")

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as con:
        if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            errors.append("Database integrity check failed")
        champions = con.execute("SELECT count(*) FROM champions").fetchone()[0]
        matchups = con.execute("SELECT count(*) FROM matchup_evidence").fetchone()[0]
        role_builds = con.execute("SELECT count(*) FROM role_builds").fetchone()[0]
        if champions < 130:
            errors.append(f"Too few champions: {champions}")
        if matchups < 17000:
            errors.append(f"Too few matchup rows: {matchups}")
        if role_builds < 160:
            errors.append(f"Too few role builds: {role_builds}")

        for table in ("champions", "items"):
            for (value,) in con.execute(f"SELECT icon_path FROM {table}"):
                text = (value or "").replace("\\", "/")
                path = resolve_media_path(text, runtime_root=root, bundle_root=asset_root)
                if not text or not path.is_file():
                    errors.append(f"{table}: {text or 'empty image path'}")
                    if len(errors) >= 25:
                        break

    images = 0
    for directory in (root / "cache", asset_root / "assets"):
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if path.suffix.lower() in (".png", ".ico", ".jpg", ".jpeg", ".webp"):
                try:
                    with Image.open(path) as image:
                        image.verify()
                    images += 1
                except Exception:
                    errors.append(f"Invalid image: {path.name}")
                    if len(errors) >= 25:
                        break

    if errors:
        raise ValueError("Incomplete distribution:\\n" + "\\n".join(errors[:25]))

    import db
    import engine
    db.init_db()
    snapshot = db.load_runtime_snapshot()
    enemies = [("Balmond", ""), ("Nana", ""), ("Miya", ""), ("Tigreal", ""), ("Saber", "")]
    picks = engine.recommend_picks("EXP", enemies, 10, snapshot=snapshot)
    if not picks:
        raise ValueError("Recommendation engine returned no picks")
    top = picks[0].get("champion") or {}
    build = engine.recommend_build(
        top.get("name") or top.get("id"), enemies, role_ru="EXP", snapshot=snapshot
    )
    if not (build.get("ordered") or build.get("base")):
        raise ValueError("Recommendation engine returned an empty build")

    return {
        "status": "ok",
        "version": VERSION,
        "champions": champions,
        "matchups": matchups,
        "role_builds": role_builds,
        "images": images,
        "top_pick": top.get("name") or top.get("id"),
        "top_matchup_sum_pp": picks[0].get("draft_matchup_sum_pp"),
    }
'''

BUILD_RELEASE_PY = r'''"""Build verified source-free MLCA 2.1.2 Windows release with Nuitka onefile."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

from diagnostics import check_installation

ROOT = Path(__file__).resolve().parent
NAME = "MobileLegendsCounterAssistant"
VERSION = "2.1.2"
WINDOWS_VERSION = "2.1.2.0"
FORBIDDEN_SUFFIXES = {".py", ".pyc", ".pyo", ".pyw", ".spec", ".c", ".cpp", ".h", ".hpp"}
FORBIDDEN_DIRS = {"components", "_internal", "__pycache__", ".venv-build", ".venv-runtime"}


def _snapshot_database(source_db: Path, target_db: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_db.resolve().as_uri() + "?mode=ro", uri=True)) as src, \
         closing(sqlite3.connect(target_db)) as dst:
        with dst:
            src.backup(dst)
            for table, column in (
                ("champions", "icon_path"),
                ("items", "icon_path"),
                ("media_assets", "local_path"),
            ):
                try:
                    rows = dst.execute(f"SELECT rowid,{column} FROM {table}").fetchall()
                except sqlite3.OperationalError:
                    continue
                for rowid, value in rows:
                    text = (value or "").replace("\\", "/")
                    if "cache/" in text:
                        text = "cache/" + text.split("cache/", 1)[1]
                    dst.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (text, rowid))


def stage_runtime_payload(source: Path, target: Path, executable: Path) -> None:
    source, target, executable = Path(source), Path(target), Path(executable)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, target / f"{NAME}.exe")

    if (source / "cache").is_dir():
        shutil.copytree(
            source / "cache",
            target / "cache",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.py", "*.pyc", "*.pyo", "*.tmp", "*.part"),
        )
    for name in ("cache/champions", "cache/items", "cache/brand", "logs", "data"):
        (target / name).mkdir(parents=True, exist_ok=True)

    source_db = source / "data/mobilelegends.db"
    if not source_db.is_file():
        raise FileNotFoundError("Missing source data/mobilelegends.db")
    _snapshot_database(source_db, target / "data/mobilelegends.db")

    for path in (source / "data").glob("*"):
        if path.is_file() and path.name != "mobilelegends.db":
            shutil.copy2(path, target / "data" / path.name)

    for name in ("VERSION.txt", "README_PORTABLE_RU.md", "CANONICAL_SOURCE.txt", "PARITY_CHECK.txt"):
        src = source / name
        if src.is_file():
            shutil.copy2(src, target / name)


def assert_source_free(target: Path) -> None:
    leaks: list[str] = []
    for path in Path(target).rglob("*"):
        relative = path.relative_to(target)
        if any(part.casefold() in FORBIDDEN_DIRS for part in relative.parts):
            leaks.append(relative.as_posix())
            continue
        if path.is_file() and path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            leaks.append(relative.as_posix())
    if leaks:
        raise ValueError("Source artifacts leaked into release:\\n" + "\\n".join(sorted(set(leaks))[:50]))


def build_nuitka_command(python: Path, output_dir: Path) -> list[str]:
    assets = ROOT / "assets"
    return [
        str(python), "-m", "nuitka",
        "--mode=onefile",
        "--assume-yes-for-downloads",
        "--mingw64",
        "--enable-plugin=tk-inter",
        "--include-package=flet",
        "--include-package-data=flet",
        "--include-package=flet_desktop",
        "--include-package-data=flet_desktop",
        "--include-package=requests",
        "--include-package=PIL",
        "--include-package-data=certifi",
        "--include-module=db",
        "--include-module=engine",
        "--include-module=draft_matrix_engine",
        "--include-module=data_contract",
        "--include-module=sources",
        "--include-module=updater",
        "--include-module=media_cache",
        "--include-module=localization",
        "--include-module=adaptive_descriptions",
        f"--include-data-dir={assets}=assets",
        "--windows-console-mode=disable",
        "--python-flag=isolated",
        "--python-flag=safe_path",
        "--python-flag=no_docstrings",
        "--include-module=PIL.ImageTk",
        f"--windows-icon-from-ico={assets / 'mobilelegends_icon.ico'}",
        f"--output-dir={Path(output_dir)}",
        f"--output-filename={NAME}.exe",
        "--product-name=Mobile Legends Counter Assistant",
        "--file-description=Mobile Legends Counter Assistant",
        f"--file-version={WINDOWS_VERSION}",
        f"--product-version={WINDOWS_VERSION}",
        "--copyright=FARLINER",
        str(ROOT / "launcher.py"),
    ]


def write_manifest(target: Path) -> None:
    files: dict[str, dict[str, int | str]] = {}
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name == "distribution.json":
            continue
        relative = path.relative_to(target)
        if "logs" in relative.parts:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[relative.as_posix()] = {"size": path.stat().st_size, "sha256": digest}
    payload = {"version": VERSION, "builder": "Nuitka 4.2.1 onefile", "files": files}
    (target / "distribution.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _find_compiled_executable(output_dir: Path) -> Path:
    expected = output_dir / f"{NAME}.exe"
    if expected.is_file():
        return expected
    matches = list(output_dir.rglob(f"{NAME}.exe"))
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"Nuitka did not produce {NAME}.exe in {output_dir}")


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("Windows release must be built on Windows.")
    version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    if version != VERSION:
        raise ValueError(f"Expected VERSION.txt={VERSION}, got {version!r}")

    print("Source preflight:", check_installation(ROOT, asset_root=ROOT), flush=True)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    build_output = ROOT / "build" / "nuitka" / stamp
    build_output.mkdir(parents=True, exist_ok=True)
    command = build_nuitka_command(Path(sys.executable), build_output)
    print("Compiling with Nuitka 4.2.1...", flush=True)
    subprocess.run(command, cwd=ROOT, check=True)

    executable = _find_compiled_executable(build_output)
    out = ROOT / "dist" / stamp
    target = out / NAME
    stage_runtime_payload(ROOT, target, executable)
    assert_source_free(target)

    print("Running compiled self-test...", flush=True)
    subprocess.run(
        [str(target / f"{NAME}.exe"), "--self-test"],
        cwd=target,
        check=True,
        timeout=180,
    )
    assert_source_free(target)
    write_manifest(target)

    archive = shutil.make_archive(
        str(out / f"{NAME}-{VERSION}-portable-free"),
        "zip",
        root_dir=out,
        base_dir=NAME,
    )
    print(f"Folder: {target}", flush=True)
    print(f"ZIP: {archive}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

BUILD_BAT = r'''@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "BUILD_LOG=%CD%\build.log"

echo [1/5] Checking Python 3.12 x64 and preparing isolated build environment...
set "BOOT_PY="
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3.12 -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8" >nul 2>&1
  if not errorlevel 1 set "BOOT_PY=py -3.12"
)
if not defined BOOT_PY (
  where python.exe >nul 2>&1
  if not errorlevel 1 (
    python.exe -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8" >nul 2>&1
    if not errorlevel 1 set "BOOT_PY=python.exe"
  )
)
if not defined BOOT_PY (
  echo [ERROR] Python 3.12 x64 is required.
  goto :error
)

if exist ".venv-build\Scripts\python.exe" (
  ".venv-build\Scripts\python.exe" -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8" >nul 2>&1
  if errorlevel 1 rmdir /s /q ".venv-build"
)
if not exist ".venv-build\Scripts\python.exe" (
  %BOOT_PY% -m venv .venv-build
  if errorlevel 1 goto :error
)
set "BUILD_PY=%CD%\.venv-build\Scripts\python.exe"

echo [2/5] Installing pinned runtime/Nuitka dependencies...
"%BUILD_PY%" -m pip install --upgrade pip > "%BUILD_LOG%" 2>&1
if errorlevel 1 goto :error
"%BUILD_PY%" -m pip install -r requirements-build.txt >> "%BUILD_LOG%" 2>&1
if errorlevel 1 goto :error

echo [3/5] Running parity and builder tests...
"%BUILD_PY%" -m unittest discover -s tests -v >> "%BUILD_LOG%" 2>&1
if errorlevel 1 goto :error

echo [4/5] Compiling Windows EXE and running compiled self-test...
"%BUILD_PY%" -u build_release.py >> "%BUILD_LOG%" 2>&1
if errorlevel 1 goto :error

echo [5/5] SUCCESS. MLCA 2.1.2 verified release is in dist\BUILD-TIME\.
type "%BUILD_LOG%"
if not defined CI pause
exit /b 0

:error
echo Build failed. No verified release was produced.
if exist "%BUILD_LOG%" type "%BUILD_LOG%"
if not defined CI pause
exit /b 1
'''

INSTALL_BAT = r'''@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title MLCA 2.1.2 - PC runtime setup

set "VENV=%CD%\.venv-runtime"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" goto :install

set "BOOT_PY="
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3.12 -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8" >nul 2>&1
  if not errorlevel 1 set "BOOT_PY=py -3.12"
)
if not defined BOOT_PY (
  where python.exe >nul 2>&1
  if not errorlevel 1 (
    python.exe -c "import sys,struct; assert sys.version_info[:2]==(3,12) and struct.calcsize('P')==8" >nul 2>&1
    if not errorlevel 1 set "BOOT_PY=python.exe"
  )
)
if not defined BOOT_PY (
  echo [ERROR] Python 3.12 x64 is required.
  pause
  exit /b 1
)
%BOOT_PY% -m venv "%VENV%"
if errorlevel 1 exit /b 1

:install
"%PY%" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo [OK] MLCA 2.1.2 PC runtime is ready.
if not defined CI pause
exit /b 0
'''

RUN_BAT = r'''@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Mobile Legends Counter Assistant 2.1.2

set "VENV=%CD%\.venv-runtime"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" goto :setup
"%PY%" -c "import flet, requests, PIL" >nul 2>&1
if errorlevel 1 goto :setup
"%PY%" launcher.py
if errorlevel 1 pause
exit /b %errorlevel%

:setup
call install.bat
if errorlevel 1 exit /b 1
"%PY%" launcher.py
if errorlevel 1 pause
exit /b %errorlevel%
'''

TEST_CORE = r'''from __future__ import annotations
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class CorePayloadTests(unittest.TestCase):
    def test_version_and_provenance(self):
        self.assertEqual((ROOT / "VERSION.txt").read_text(encoding="utf-8").strip(), "2.1.2")
        manifest = json.loads((ROOT / "SOURCE_HASHES.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_commit"], "63837d7c0bd1a258c7984746a35256de0e273556")
        self.assertEqual(manifest["apk_run_id"], "36661885778")

    def test_database_is_exact_apk_seed(self):
        manifest = json.loads((ROOT / "SOURCE_HASHES.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256((ROOT / "data/mobilelegends.db").read_bytes()).hexdigest()
        self.assertEqual(digest, manifest["apk_database_sha256"])

    def test_database_shape(self):
        with sqlite3.connect(ROOT / "data/mobilelegends.db") as con:
            self.assertEqual(con.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertGreaterEqual(con.execute("SELECT count(*) FROM champions").fetchone()[0], 130)
            self.assertGreaterEqual(con.execute("SELECT count(*) FROM matchup_evidence").fetchone()[0], 17000)
            self.assertGreaterEqual(con.execute("SELECT count(*) FROM role_builds").fetchone()[0], 160)

    def test_runtime_core_hashes(self):
        manifest = json.loads((ROOT / "SOURCE_HASHES.json").read_text(encoding="utf-8"))
        for name, expected in manifest["core_source_sha256"].items():
            digest = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            self.assertEqual(digest, expected, name)

if __name__ == "__main__":
    unittest.main()
'''

TEST_PC = r'''from __future__ import annotations
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class PCHostTests(unittest.TestCase):
    def test_app_hosts_mobile_assistant_and_exact_version_ui(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        funcs = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertIn("MobileAssistant", classes)
        self.assertIn("run", funcs)
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('"draft_matchup": "Матчап"', src)
        self.assertNotIn('"Матчап Σ"', src)

    def test_builder_is_pinned_to_python_312_and_nuitka(self):
        bat = (ROOT / "build_exe.bat").read_text(encoding="utf-8")
        req = (ROOT / "requirements-build.txt").read_text(encoding="utf-8")
        self.assertIn("Python 3.12", bat)
        self.assertIn("Nuitka==4.2.1", req)

    def test_builder_includes_new_212_core_modules(self):
        src = (ROOT / "build_release.py").read_text(encoding="utf-8")
        for module in ("draft_matrix_engine", "data_contract", "engine", "db", "sources", "updater"):
            self.assertIn(f"--include-module={module}", src)

    def test_source_tree_has_no_generated_build_junk(self):
        for relative in (".venv-runtime", "build", "dist"):
            self.assertFalse((ROOT / relative).exists(), relative)

if __name__ == "__main__":
    unittest.main()
'''

TEST_RUNTIME = r'''from __future__ import annotations
import unittest

import db
import engine

class RuntimeRecommendationTests(unittest.TestCase):
    def test_full_five_enemy_draft_returns_10_ranked_counter_candidates(self):
        db.init_db()
        snapshot = db.load_runtime_snapshot()
        enemies = [("Balmond", ""), ("Nana", ""), ("Miya", ""), ("Tigreal", ""), ("Saber", "")]
        picks = engine.recommend_picks("EXP", enemies, 10, snapshot=snapshot)
        self.assertEqual(len(picks), 10)
        sums = [float(p.get("draft_matchup_sum_pp") or 0.0) for p in picks]
        self.assertEqual(sums, sorted(sums, reverse=True))
        for pick in picks:
            self.assertTrue(pick.get("positive"), pick.get("champion", {}).get("name"))

    def test_top_pick_build_is_nonempty(self):
        snapshot = db.load_runtime_snapshot()
        enemies = [("Balmond", ""), ("Nana", ""), ("Miya", ""), ("Tigreal", ""), ("Saber", "")]
        pick = engine.recommend_picks("EXP", enemies, 1, snapshot=snapshot)[0]
        champ = pick["champion"]
        build = engine.recommend_build(champ.get("name") or champ["id"], enemies, role_ru="EXP", snapshot=snapshot)
        self.assertTrue(build.get("ordered") or build.get("base"))

if __name__ == "__main__":
    unittest.main()
'''

def copy_apk_payload(apk_app: Path, target: Path) -> str:
    assets = apk_app / "assets"
    if not assets.is_dir():
        raise RuntimeError("APK app.zip has no assets directory")

    target_assets = target / "assets"
    target_cache = target / "cache"
    target_data = target / "data"
    target_assets.mkdir(parents=True, exist_ok=True)

    for child in assets.iterdir():
        if child.name in {"cache", "data"}:
            continue
        dest = target_assets / child.name
        if child.is_dir():
            shutil.copytree(child, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(child, dest)

    shutil.copytree(assets / "cache", target_cache, dirs_exist_ok=True)
    shutil.copytree(assets / "data", target_data, dirs_exist_ok=True)
    (target / "logs").mkdir(exist_ok=True)

    ico = target_assets / "mobilelegends_icon.ico"
    if not ico.is_file():
        from PIL import Image
        png = target_assets / "mobilelegends_icon.png"
        with Image.open(png) as image:
            image.save(ico, format="ICO", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
    return sha256(target_data / "mobilelegends.db")

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--apk-artifact-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    out_root = args.output_dir.resolve()
    work = out_root / "_work"
    if out_root.exists():
        shutil.rmtree(out_root)
    work.mkdir(parents=True)

    apk_app = extract_apk_app(args.apk_artifact_dir.resolve(), work)
    target = out_root / PROJECT_DIRNAME
    target.mkdir(parents=True)

    core_hashes: dict[str, str] = {}
    for name in CORE_FILES:
        src = repo_root / "mlbb" / name
        if not src.is_file():
            raise FileNotFoundError(src)
        shutil.copy2(src, target / name)
        core_hashes[name] = sha256(src)

    android_main = (repo_root / "mlbb/main.py").read_text(encoding="utf-8")
    android_main_hash = hashlib.sha256(android_main.encode("utf-8")).hexdigest()
    (target / "app.py").write_text(desktop_app(android_main), encoding="utf-8", newline="\n")
    (target / "paths.py").write_text(PATHS_PY, encoding="utf-8", newline="\n")
    (target / "launcher.py").write_text(LAUNCHER_PY, encoding="utf-8", newline="\n")
    (target / "diagnostics.py").write_text(DIAGNOSTICS_PY, encoding="utf-8", newline="\n")
    (target / "build_release.py").write_text(BUILD_RELEASE_PY, encoding="utf-8", newline="\n")
    (target / "build_exe.bat").write_text(BUILD_BAT, encoding="utf-8", newline="\r\n")
    (target / "install.bat").write_text(INSTALL_BAT, encoding="utf-8", newline="\r\n")
    (target / "run.bat").write_text(RUN_BAT, encoding="utf-8", newline="\r\n")
    (target / "VERSION.txt").write_text(VERSION + "\n", encoding="utf-8")

    (target / "requirements.txt").write_text(
        "requests>=2.32.0\nPillow>=10.4.0\nflet[desktop]==1.0.1\n",
        encoding="utf-8",
    )
    (target / "requirements-build.txt").write_text(
        "-r requirements.txt\nNuitka==4.2.1\nzstandard==0.25.0\n",
        encoding="utf-8",
    )

    db_digest = copy_apk_payload(apk_app, target)

    hashes = {
        "version": VERSION,
        "source_commit": SOURCE_COMMIT,
        "apk_run_id": APK_RUN_ID,
        "apk_artifact_id": APK_ARTIFACT_ID,
        "apk_artifact_name": APK_ARTIFACT_NAME,
        "android_main_sha256": android_main_hash,
        "core_source_sha256": core_hashes,
        "apk_database_sha256": db_digest,
    }
    (target / "SOURCE_HASHES.json").write_text(
        json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    canonical = f"""Mobile Legends Counter Assistant {VERSION} — PC Builder Free

ANDROID REFERENCE (canonical):
Repository: thepurelydudes-max/Android-APK
Source commit: {SOURCE_COMMIT}
GitHub Actions run: {APK_RUN_ID}
Artifact: {APK_ARTIFACT_NAME}
Artifact id: {APK_ARTIFACT_ID}
APK database SHA-256: {db_digest}

db.py, engine.py, draft_matrix_engine.py, data_contract.py, sources.py,
updater.py, media_cache.py, localization.py and adaptive_descriptions.py
are copied byte-for-byte from the source commit used by MLCA 2.1.2.

app.py is that same mlbb/main.py with desktop-shell-only changes:
- portable Windows resource_path import;
- 450x800 desktop window/icon;
- run(initial_lang) Flet Desktop entry.

paths.py, launcher.py, diagnostics.py and build files are PC-only infrastructure.
No licensing gate is added.
"""
    (target / "CANONICAL_SOURCE.txt").write_text(canonical, encoding="utf-8")

    parity = f"""MLCA 2.1.2 PC BUILDER — PARITY CHECK

Exact APK reference:
  run {APK_RUN_ID}
  artifact {APK_ARTIFACT_NAME} ({APK_ARTIFACT_ID})
  source commit {SOURCE_COMMIT}
  database SHA-256 {db_digest}

The PC project uses the same recommendation/update core and the exact database,
localization and cached hero/item media extracted from the successful APK.
Only the host/runtime filesystem and Windows Flet shell are changed.

Builder contract:
  Python 3.12 x64
  Flet Desktop 1.0.1
  Nuitka 4.2.1 onefile
  compiled EXE self-test is mandatory
  release contains no .py/.pyc/.spec/C/C++ source files
"""
    (target / "PARITY_CHECK.txt").write_text(parity, encoding="utf-8")

    readme = f"""# MLCA {VERSION} — PC Builder Free

Это ПК-перенос именно успешного APK MLCA {VERSION}.

## Запустить на ПК из исходников
1. Нужен Python 3.12 x64.
2. Запусти run.bat.
3. При первом запуске локально создастся .venv-runtime и установится Flet Desktop.

## Собрать EXE
Запусти build_exe.bat.

Builder сам:
- проверяет Python 3.12 x64;
- создаёт отдельную .venv-build;
- ставит Flet Desktop + Nuitka 4.2.1;
- запускает тесты parity/runtime/builder;
- собирает one-file MobileLegendsCounterAssistant.exe;
- создаёт portable-папку с внешними data/ и cache/;
- запускает скомпилированный EXE с --self-test;
- только после успешного self-test создаёт ZIP в dist\\дата-время\\.

В ПК-версии сохранены база, matchup-логика, относительное Контрит,
суммарный Матчап, билд-рекомендации, updater, локализация и media cache
из APK {VERSION}. ПК-обвязка меняет только файловые пути, окно и упаковку.

Точная привязка к APK находится в CANONICAL_SOURCE.txt и SOURCE_HASHES.json.
"""
    (target / "README_PORTABLE_RU.md").write_text(readme, encoding="utf-8")

    tests = target / "tests"
    tests.mkdir()
    (tests / "test_core_payload.py").write_text(TEST_CORE, encoding="utf-8")
    (tests / "test_pc_host.py").write_text(TEST_PC, encoding="utf-8")
    (tests / "test_runtime_recommendations.py").write_text(TEST_RUNTIME, encoding="utf-8")

    import py_compile
    for path in target.glob("*.py"):
        py_compile.compile(str(path), doraise=True)
    for path in tests.glob("*.py"):
        py_compile.compile(str(path), doraise=True)
    for pycache in target.rglob("__pycache__"):
        shutil.rmtree(pycache)

    shutil.rmtree(work)
    print(target)
    print(json.dumps(hashes, ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
