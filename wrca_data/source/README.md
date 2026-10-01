# GPT-prepared WRCA data source

The package workflow starts from the currently published, already verified WRCA data release.
This folder contains only the changes that should be applied on top of that known-good package.

Future GPT update workflow:

1. Prepare database changes in update.sql (or edit apply_update.py for a specialized collector).
2. Put media replacements under cache_overrides/ using paths relative to cache/.
3. Put obsolete cache paths in delete_cache.txt.
4. For external binary resources, add HTTPS URL + target path + SHA-256 to download_cache.json.
5. Trigger Build WRCA Data Package.

The workflow validates the resulting database/cache before publishing a new immutable GitHub Release.
If any validation fails, wrca_data/latest.json is not changed, so installed APKs stay on the last known-good package.

Do not change protocol_version unless the APK updater protocol itself changes.
