# MLCA package source overrides

Normal MLCA packages are refreshed on GitHub with mlbb/prepare_bundled_seed.py.

Optional repository-side overrides can be prepared here before publishing:
- update.sql — SQLite patch statements
- cache_overrides/ — files copied over the refreshed cache
- delete_cache.txt — cache paths to remove
- download_cache.json — HTTPS cache downloads with mandatory SHA-256

The Android APK never runs these source scripts. They are GitHub package-builder inputs only.
