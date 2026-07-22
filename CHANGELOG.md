# Changelog

All notable changes are documented here. The project follows Semantic
Versioning after `1.0`; pre-1.0 releases may contain breaking changes.

## 0.2.0-alpha.1 - 2026-07-22

- Added a guided Safety Center for setup and release diagnostics.
- Added credential-free named lineup snapshots, diffs, atomic restore, action
  history, automatic pre-change safety points, and one-click undo.
- Added non-mutating source refresh previews with locked-removal warnings.
- Added bulk placement locking for stations that must never drift.
- Added structural JSON backup validation before restore.
- Added XMLTV quality scanning for empty channels, blank titles, and generic
  programme names.
- Added Teamarr readiness reporting and expanded live-only compatibility tests.
- Added schema-v3 clean-install and upgrade migration coverage.
- Split new backend and frontend workflows into dedicated feature modules.

## 0.1.0-alpha.1 - 2026-07-22

- First sanitized public alpha.
- Portable named-volume Docker Compose deployment.
- Secure-by-default administrator authentication and restricted CORS.
- Stable channel identity, numbering, placement locks, and provider revival.
- Multi-source M3U/Xtream imports, rules, XMLTV exports, and guide diagnostics.
- Experimental live-TV Teamarr support with stable numeric IDs and EPG actions.
- Optional caching edge and Dispatcharr XMLTV snapshot integration.
