# Windows PC builders

`pc/make_builder.py` is the single source of truth for the Windows Builder layer.
It does **not** duplicate APK assets/data in Git. For each product it:

1. takes the current `wild_rift/` or `mlbb/` source used by the APK;
2. downloads the current verified `wrca_data` / `mlca_data` release package and checks SHA-256 + SQLite;
3. adds the Windows-safe deferred updater (no live SQLite/cache replacement while Flet is running);
4. for MLCA adds the proxy/VPN fallback without disabling TLS verification;
5. produces a self-contained PC Builder ZIP;
6. GitHub Actions builds and self-tests a one-file EXE and publishes a Portable ZIP.

## Normal command workflow

To request fresh Windows builds, change `pc/build-request.txt` and push to `main`.
That starts **Build PC Portables** on `windows-latest` for WRCA and MLCA in parallel.
The result contains a Builder ZIP and a ready-to-run Portable ZIP with EXE for each product.

Old files deleted from `main` remain available in Git history; repository cleanup does not rewrite history.
