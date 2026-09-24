# FARLINER Android APK builds

This repository builds Android APK files for:

- Wild Rift Counter Assistant
- Mobile Legends Counter Assistant

## One-time source import

Upload these two archives to the repository root **without renaming them**:

- `SRC WILD RIFT.zip`
- `SRC_MLBB_1.0.5_REWORKED.zip`

The workflow **Import full SRC archives** starts automatically after the upload. It unpacks the archives and commits the complete projects to:

- `wild_rift/`
- `mlbb/`

All bundled PNG images, databases, cache assets, icons and Python files are kept.

## Build APK

Open **Actions → Build Android APK → Run workflow** and choose:

- `wild_rift`
- `mlbb`
- `both`

The finished APK build is saved as a GitHub Actions artifact named `FARLINER-...-APK`.

## Toolchain

- Ubuntu GitHub runner
- Python 3.12
- Java 17
- Flet CLI 1.0.1
- Flutter / Android SDK prepared by Flet as required
