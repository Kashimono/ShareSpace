# Changelog

## 1.0.0 — Release candidate

- Added Windows x64 and Linux x86_64 one-folder package builds with local smoke checks.
- Added per-user Windows and Linux install/uninstall scripts. Uninstall leaves settings,
  transfer history, and session history in the OS application-data directory.
- Added application icons, version metadata, rotating local diagnostic logs, and
  credential redaction for persistent logs.
- Added SQLite integrity checks, safe schema upgrade hooks, and pre-migration backups.
- Made session reconnect backoff stop promptly during application shutdown.
- Added native Windows and Ubuntu 22.04 package CI; workflow artifacts are temporary
  build outputs and are not published as a public release.
- Kept Windows hotspot control unavailable in the unpackaged application build.

Earlier milestone notes are preserved in the project history and the 0.7.0 feature
overview in the README.
