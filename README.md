# FARLINER Counter Assistants

Current projects:

- `wild_rift/` — WRCA Android/application source.
- `mlbb/` — MLCA Android/application source.
- `wrca_data/` — verified WRCA GitHub data-package pipeline.
- `mlca_data/` — verified MLCA GitHub data-package pipeline.
- `pc/` — Windows Builder generator and build trigger.

## Android

Use **Actions → Build Android APK** to build the current APK versions.

## Windows PC

Windows EXE files are built on GitHub `windows-latest`, so a local PC does not need to run Nuitka.
A change to `pc/build-request.txt` starts **Build PC Portables**, which builds WRCA and MLCA in parallel and produces for each application:

- a current PC Builder ZIP;
- a verified Portable ZIP containing the ready-to-run EXE.

The PC Builder is generated from the same current application source used for the APK plus a small Windows-specific layer for filesystem/update behavior. Data/cache are restored from the current verified GitHub data package instead of being duplicated in Git.

## Repository history

Obsolete source-import archives, retired WLCA package experiments, old broken PC workflows and WRCA `BUILD_LOGIC_*` notes below 3.8.1 were removed from the current `main` tree. Their history remains available in Git commits; history was not rewritten.
