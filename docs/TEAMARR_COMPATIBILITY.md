# Teamarr compatibility

M3U Boss implements a deliberately small, live-TV-only subset of the Xtream
Codes API. The compatibility surface is covered by automated contract tests so
lineup work cannot silently change identifiers or response shapes.

## Supported contract

| Surface | Behavior |
| --- | --- |
| Account info | Active user and externally configured server information |
| `get_live_categories` | Enabled groups explicitly marked for Teamarr |
| `get_live_streams` | Stable numeric stream/category IDs and stable player numbers |
| `get_short_epg` | Base64 title/description and timestamped future listings |
| `get_simple_data_table` | Same live EPG contract used by common clients |
| `/live/...` | Authenticated proxy with configured stream failover |
| `/xmltv.php`, `/teamarr.xml` | Authenticated XMLTV output |
| VOD and series actions | Valid empty arrays |

VOD catalogs, series catalogs, catch-up archives, recording, and connection
limit enforcement are not implemented. Returning empty collections is
intentional and prevents clients from treating unsupported features as server
errors.

## Client testing

For a compatibility report, include the client name and version, platform,
whether categories and current listings load, and the failing API action from a
sanitized diagnostic bundle. Never attach a database, JSON backup, provider
URL, or Teamarr password to a public issue.
