from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VERSION = (ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
DIST = ROOT / "dist"
RELEASE_ROOT = ROOT / "release" / "WildRiftCounterAssistant"


def copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    exe = DIST / "WildRiftCounterAssistant.exe"
    if not exe.is_file():
        raise FileNotFoundError(
            "dist/WildRiftCounterAssistant.exe not found. Run flet pack first."
        )

    if RELEASE_ROOT.exists():
        shutil.rmtree(RELEASE_ROOT)
    RELEASE_ROOT.mkdir(parents=True, exist_ok=True)

    shutil.copy2(exe, RELEASE_ROOT / exe.name)
    shutil.copy2(ROOT / "VERSION.txt", RELEASE_ROOT / "VERSION.txt")
    shutil.copy2(
        ROOT / "README_PORTABLE_RU.md",
        RELEASE_ROOT / "README_PORTABLE_RU.md",
    )

    assets = ROOT / "assets"
    copy_tree(assets / "data", RELEASE_ROOT / "data")
    copy_tree(assets / "cache", RELEASE_ROOT / "cache")
    copy_tree(assets / "roles", RELEASE_ROOT / "roles")

    for name in (
        "wildrift_icon.png",
        "wildrift_logo.png",
        "icon.png",
    ):
        src = assets / name
        if src.is_file():
            shutil.copy2(src, RELEASE_ROOT / name)

    (RELEASE_ROOT / "logs").mkdir(parents=True, exist_ok=True)

    files = {}
    for path in sorted(RELEASE_ROOT.rglob("*")):
        if not path.is_file() or path.name == "distribution.json":
            continue
        rel = path.relative_to(RELEASE_ROOT).as_posix()
        files[rel] = {
            "size": path.stat().st_size,
            "sha256": file_sha256(path),
        }

    manifest = {
        "version": VERSION,
        "platform": "Windows 10/11 x64",
        "builder": "Flet 1.0.1 flet pack / PyInstaller onefile",
        "shared_logic": [
            "draft_matrix_engine.py",
            "engine.py",
            "db.py",
            "updater.py",
            "adaptive_descriptions.py",
        ],
        "files": files,
    }
    (RELEASE_ROOT / "distribution.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    archive_base = ROOT / "release" / f"WildRiftCounterAssistant-{VERSION}-Portable-FREE"
    archive = Path(shutil.make_archive(
        str(archive_base),
        "zip",
        root_dir=RELEASE_ROOT.parent,
        base_dir=RELEASE_ROOT.name,
    ))
    print(f"Portable folder: {RELEASE_ROOT}")
    print(f"Archive: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
