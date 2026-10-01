# WLCA Data Packages

This directory is the data-update side of WLCA 3.9.0+.

The Android APK does not scrape WildRiftCore, WR Pocket, WildRiftCounter or other game-data sites.
It reads only:

https://raw.githubusercontent.com/thepurelydudes-max/Android-APK/main/wlca_data/latest.json

and downloads exactly one immutable ZIP from this repository's GitHub Releases.

## Stable protocol

Protocol version 1 contains:

- manifest.json
- data/wildrift.db
- cache/champions/*.png
- cache/items/*.png
- optional cache/brand/*

A package is published only after SQLite, row-count, WRC page-cache and media coverage checks pass.
The APK verifies the release ZIP SHA-256, the internal manifest SHA-256, the database SHA-256,
PRAGMA quick_check, exact manifest counts and media counts before replacing its current data.

A failed or interrupted download never replaces the installed database. The partial ZIP remains resumable.
Promotion has a rollback marker so an Android process kill during the final swap restores the previous working set next time.

## Building a new package

Run the GitHub Actions workflow Build WLCA Data Package. GPT can prepare or update the database/cache
sources in the repository and trigger the same workflow. The workflow creates a versioned GitHub Release
and only after that updates wlca_data/latest.json.

As long as future data packages stay on protocol 1, users do not need to reinstall the APK just to receive database/resource updates.
