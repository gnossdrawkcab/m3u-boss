# Security policy

## Supported versions

Only the newest alpha release receives security fixes while the project is
pre-1.0.

## Reporting a vulnerability

Do not open a public issue containing a vulnerability, playlist URL, backup,
database, provider credential, integration token, or diagnostic output with
secrets. Use GitHub's private vulnerability reporting feature for this
repository. If it is unavailable, open a minimal issue asking the maintainer to
enable a private reporting channel without including exploit details.

## Deployment expectations

- Set a long random `M3U_BOSS_ADMIN_PASSWORD`; startup refuses management access
  without it unless the explicit development-only insecure switch is enabled.
- Keep the app on a trusted LAN/VPN or behind an HTTPS reverse proxy.
- Restrict CORS to exact trusted origins when cross-origin access is required.
- Treat SQLite databases, exports, logs, and JSON backups as secrets. Provider
  credentials may be embedded in stream URLs.
- Rotate credentials immediately if any runtime artifact is committed or
  attached to an issue.
