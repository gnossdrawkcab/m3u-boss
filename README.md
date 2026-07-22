# M3U Boss

M3U Boss is a self-hosted editor for large IPTV lineups. It imports M3U or
Xtream Codes live-TV sources, preserves channel identity and player numbers
across provider refreshes, organizes channels with opt-in rules, and exports a
curated M3U and XMLTV guide.

> **Alpha software:** back up your data, review rule previews, and test exports
> in a secondary player before replacing a production lineup.

## Highlights

- Multiple simultaneous M3U and Xtream live-TV sources
- Stable channel identity, placement locks, tombstone revival, and player numbers
- Safety Center with import previews, named snapshots, action history, and undo
- Fast lazy-loaded desktop and mobile channel editor with current listings
- Exact, contains, prefix, and regular-expression organization rules
- XMLTV aggregation, matching, diagnostics, dummy EPG, and filtered exports
- Optional Dispatcharr-aware caching edge
- Experimental live-TV Teamarr/Xtream compatibility
- Automated SQLite snapshots and JSON backup/restore

## Quick start

Requirements: Docker with Compose v2.

```bash
cp .env.example .env
openssl rand -base64 32
# Put the generated value in M3U_BOSS_ADMIN_PASSWORD in .env
docker compose up -d --build
```

Open <http://localhost:43817> and sign in with the username and password from
`.env`. Change `M3U_BOSS_PUBLIC_URL` when players use another hostname or LAN IP.

M3U Boss uses named volumes, so upgrades do not remove its database or exports.
Never run two instances against the same SQLite database.

## Workflow

1. Add an M3U or Xtream source on **Sources**.
2. Review imported groups and disable anything you do not want.
3. Create rules only for stable provider labels. Preview before applying them.
4. Lock hand-curated channel names or placements that must survive refreshes.
5. Export, then give your player the displayed M3U and XMLTV URLs.

The **Safety Center** guides a new installation, previews source churn without
applying it, validates JSON backups, scans guide quality, and records automatic
undo points before high-impact lineup operations. Its lineup snapshots never
contain source URLs, provider credentials, or integration settings.

Unmatched channels stay where they are. Rules do not silently dump the rest of
the lineup into a catch-all group.

## Teamarr (experimental)

Teamarr exposes M3U Boss as a live-TV-only Xtream server. Enable it in the UI,
mark the groups to publish, and use the displayed server and credentials.

The alpha supports account info, live categories, live streams, XMLTV,
`get_short_epg`, and `get_simple_data_table`. It returns valid empty collections
for VOD and series calls. Category and stream IDs are stable numeric mappings,
player numbers come from M3U Boss's persistent channel map, and playback uses
M3U Boss's stream proxy and configured failover URLs. It does not provide VOD,
series libraries, catch-up, or connection-limit enforcement.

Please report the player name/version with Teamarr compatibility bugs. TiviMate,
Smarters, XCIPTV, Televizo, and other clients do not interpret every Xtream
response identically.

See [docs/TEAMARR_COMPATIBILITY.md](docs/TEAMARR_COMPATIBILITY.md) for the tested
contract, deliberately unsupported surfaces, and a client-report checklist.

For support issues, download `/api/system/diagnostics-bundle`. It omits source
names, URLs, usernames, passwords, and tokens. Review it before attaching it;
never substitute a database or full JSON backup.

## Optional caching edge and Dispatcharr

The normal Compose file is the portable core installation. To add the Go edge:

```bash
docker compose -f compose.edge.yaml up -d --build
```

Set `DISPATCHARR_EPG_URL` only if you want the edge to snapshot a slow
Dispatcharr XMLTV endpoint. Dispatcharr is an optional downstream consumer;
M3U Boss does not require it. Cloudflare Access header gating is also optional,
while the M3U Boss administrator password remains the primary protection.

## Backups and upgrades

M3U Boss keeps compressed SQLite snapshots under the data volume. The JSON
backup endpoint contains source URLs, provider passwords, integration tokens,
and other secrets. Treat every backup as a credential file and never attach one
to a public issue.

Before upgrading:

1. Download a JSON backup and store it securely.
2. Stop the container.
3. Back up the Docker data volume.
4. Pull the new release and run `docker compose up -d --build`.
5. Check `/healthz`, the diagnostics screen, exports, and one test channel.

Database changes are additive and recorded in `schema_meta`. Downgrades are not
automatically supported.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock
pip install -r requirements-dev.txt
M3U_BOSS_ADMIN_PASSWORD=test-only-password pytest -q
```

Browser tests use Playwright:

```bash
npm install
npx playwright install chromium
M3U_BOSS_ADMIN_PASSWORD=test-only-password uvicorn app.main:app --port 43817
npm run test:e2e
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Household-specific deployments
should follow [docs/PERSONAL_DEPLOYMENTS.md](docs/PERSONAL_DEPLOYMENTS.md).

## Security and legal notice

Do not expose M3U Boss directly to the public internet. Use a maintained HTTPS
reverse proxy or VPN and keep the admin password enabled. M3U Boss does not
provide or endorse media sources; users are responsible for having permission
to access and redistribute their configured streams and guide data.

Released under the MIT License.
