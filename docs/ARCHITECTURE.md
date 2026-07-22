# Architecture

M3U Boss has a FastAPI application, a static browser client, SQLite state, and
an optional Go caching edge.

- `app/main.py` remains the composition root for core HTTP routes, exports,
  guide assembly, background refreshes, integrations, and stream proxying.
- `app/db.py` owns schema initialization, additive migrations, persistence, and
  stable identity maps.
- `app/importers.py`, `app/rules.py`, and `app/epg.py` isolate import, matching,
  and XMLTV behavior.
- `app/history.py` owns compact credential-free lineup snapshots, diffs, action
  history, and atomic restore/undo.
- `app/release_features.py` is an independent router for setup guidance, import
  review, backup validation, EPG quality, migrations, and compatibility checks.
- `app/static/` contains the dependency-free browser UI.
- `app/static/release-features.js` owns the Safety Center workflow. New UI
  features should receive a module rather than extending the legacy editor
  bundle; existing sections can migrate incrementally without a flag day.
- `edge/` serves generated feeds efficiently and can snapshot Dispatcharr XMLTV.

SQLite runs in WAL mode. Use local container/host storage and exactly one writer;
network shares and two containers pointed at the same database are unsupported.
Generated exports are derived state and can be rebuilt from the database and EPG
sources. The database and JSON backups may contain credentials.

Lineup snapshots are intentionally narrower than backups. They contain groups,
channels, rules, stable number maps, and tombstones, but exclude sources,
settings, and credentials. High-impact mutation requests capture a bounded
automatic snapshot before execution and append an action record afterward.

Integrations are optional boundaries. The core application starts without
Dispatcharr, Tunarr, Teamarr, Cloudflare, or an external Docker network.
