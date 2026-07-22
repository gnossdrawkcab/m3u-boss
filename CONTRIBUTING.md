# Contributing

Thank you for helping improve M3U Boss. For substantial changes, start with an
issue describing the user problem and compatibility impact.

## Pull requests

1. Do not include real provider URLs, credentials, databases, playlists, EPG
   dumps, private IPs, or personally curated lineups.
2. Add regression tests for behavior changes.
3. Run `pytest -q`, the Playwright suite for UI changes, and `docker compose
   config` before submitting.
4. Preserve channel IDs, channel numbers, user locks, and group placement during
   refreshes unless a migration explicitly documents otherwise.
5. Keep integrations optional. A clean core installation must not require
   Dispatcharr, Tunarr, Teamarr, Cloudflare, or a pre-existing Docker network.

Commits should explain why a change is needed. By contributing, you agree that
your contribution is licensed under the repository's MIT License.
