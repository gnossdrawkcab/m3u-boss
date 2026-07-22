# Architecture

M3U Boss has a FastAPI application, a static browser client, SQLite state, and
an optional Go caching edge.

- `app/main.py` owns HTTP routes, exports, guide assembly, background refreshes,
  integrations, and stream proxying.
- `app/db.py` owns schema initialization, additive migrations, persistence, and
  stable identity maps.
- `app/importers.py`, `app/rules.py`, and `app/epg.py` isolate import, matching,
  and XMLTV behavior.
- `app/static/` contains the dependency-free browser UI.
- `edge/` serves generated feeds efficiently and can snapshot Dispatcharr XMLTV.

SQLite runs in WAL mode. Use local container/host storage and exactly one writer;
network shares and two containers pointed at the same database are unsupported.
Generated exports are derived state and can be rebuilt from the database and EPG
sources. The database and JSON backups may contain credentials.

Integrations are optional boundaries. The core application starts without
Dispatcharr, Tunarr, Teamarr, Cloudflare, or an external Docker network.
