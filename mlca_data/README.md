# MLCA Data Packages

MLCA 2.1.7+ updates its database and dynamic cache only from verified GitHub Release packages.

The Android app reads:
https://raw.githubusercontent.com/thepurelydudes-max/Android-APK/main/mlca_data/latest.json

It does not scrape MLBBHub, Rone, MLBBDex or Moonton data sources on the phone. GitHub Actions builds the package, validates SQLite and matchup direction, publishes an immutable release asset, then updates latest.json.

Protocol 1 package contents:
- manifest.json
- data/mobilelegends.db
- cache/champions/*.png
- cache/items/*.png

The APK verifies release SHA-256, internal manifest SHA-256, SQLite quick_check, exact row counts, matchup contract v2, matchup source/direction sentinels, and cache hashes before promotion. Interrupted promotion rolls back to the previous working set.

To prepare a package, update mlca_data/source if needed and trigger the Build MLCA Data Package workflow. The workflow first refreshes the verified MLCA seed on GitHub, then packages and publishes it.
