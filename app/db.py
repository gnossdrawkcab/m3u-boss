"""
SQLite-backed data layer for M3U Boss.
All channel, group, rule, and source data stored in SQLite for performance.
"""

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sources.db"


def _db_path_str() -> str:
    """Return a SQLite-compatible path string.

    SQLite on Windows cannot use UNC paths (\\\\server\\share) due to
    locking limitations. If the resolved path is UNC, we fall back to
    using a mapped drive letter or the APPDATA-based local path.
    """
    p = str(DB_PATH)
    # On Windows, if path starts with \\\\, SQLite can't use it directly.
    # Try the environment-override first, then the raw path.
    override = os.environ.get("M3U_BOSS_DB")
    if override:
        Path(override).parent.mkdir(parents=True, exist_ok=True)
        return override
    return p


# Thread-local connection pool — reuse one connection per thread instead of
# opening/closing + running PRAGMAs on every single call.
_local = threading.local()
_teamarr_id_lock = threading.Lock()

def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    requested_db = _db_path_str()
    if conn is not None and getattr(_local, "db_path", None) != requested_db:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None
        conn = None
    if conn is not None:
        try:
            # A transient bind-mount/storage error can poison a long-lived
            # SQLite handle. Touch the schema before reusing it so the next
            # request replaces that handle instead of returning endless 500s.
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            _local.conn = None
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = requested_db
    # Network-backed paths can exhibit longer lock windows.
    # Use a longer connect timeout and busy timeout to reduce transient
    # 'database is locked' failures during editor channel pagination.
    last_error = None
    for attempt in range(2):
        conn = None
        try:
            conn = sqlite3.connect(db, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
            conn.execute("PRAGMA cache_size=-8000")     # 8 MB page cache
            # Avoid a second application-level mmap on the bind-mounted DB.
            # WAL manages its own shared-memory file; page-cache reads are
            # plenty for this ~30 MB database and recover more cleanly.
            conn.execute("PRAGMA mmap_size=0")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
            _local.conn = conn
            _local.db_path = db
            return conn
        except sqlite3.Error as exc:
            last_error = exc
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            if attempt == 0:
                time.sleep(0.05)
    raise last_error


def connection() -> sqlite3.Connection:
    """Return the current thread's configured database connection.

    Feature modules use this small public boundary instead of reaching into the
    connection-pool implementation. Callers must commit or roll back writes.
    """
    return _conn()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            name                TEXT    NOT NULL DEFAULT '',
            type                TEXT    NOT NULL DEFAULT '',
            xc_server           TEXT    NOT NULL DEFAULT '',
            xc_username         TEXT    NOT NULL DEFAULT '',
            xc_password         TEXT    NOT NULL DEFAULT '',
            xc_output           TEXT    NOT NULL DEFAULT 'ts',
            m3u_url             TEXT    NOT NULL DEFAULT '',
            epg_url             TEXT    NOT NULL DEFAULT '',
            is_active           INTEGER NOT NULL DEFAULT 0,
            auto_refresh        INTEGER NOT NULL DEFAULT 0,
            refresh_hours       INTEGER NOT NULL DEFAULT 24,
            last_refreshed      TEXT    DEFAULT NULL,
            expires_at          TEXT    DEFAULT NULL,
            max_connections     INTEGER DEFAULT NULL,
            active_connections  INTEGER DEFAULT NULL,
            status              TEXT    NOT NULL DEFAULT '',
            priority            INTEGER NOT NULL DEFAULT 10,
            created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS groups_ (
            id           TEXT    PRIMARY KEY,
            name         TEXT    NOT NULL,
            enabled      INTEGER NOT NULL DEFAULT 1,
            pinned       INTEGER NOT NULL DEFAULT 0,
            is_custom    INTEGER NOT NULL DEFAULT 0,
            is_unmatched INTEGER NOT NULL DEFAULT 0,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            teamarr      INTEGER NOT NULL DEFAULT 0,
            name_epg     INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS channels (
            id           TEXT    PRIMARY KEY,
            source_id    INTEGER NOT NULL,
            stream_id    INTEGER,
            name         TEXT    NOT NULL DEFAULT 'Unnamed',
            source_name  TEXT    NOT NULL DEFAULT '',
            source_group TEXT    NOT NULL DEFAULT 'Ungrouped',
            group_id     TEXT,
            tvg_id       TEXT    DEFAULT '',
            original_tvg_id TEXT DEFAULT '',
            tvg_name     TEXT    DEFAULT '',
            logo         TEXT    DEFAULT '',
            url          TEXT    NOT NULL DEFAULT '',
            enabled      INTEGER NOT NULL DEFAULT 1,
            favorite     INTEGER NOT NULL DEFAULT 0,
            sort_order   INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS rules (
            id          TEXT    PRIMARY KEY,
            group_id    TEXT    NOT NULL,
            field       TEXT    NOT NULL DEFAULT 'source_group',
            pattern     TEXT    NOT NULL DEFAULT '',
            match_type  TEXT    NOT NULL DEFAULT 'contains',
            sort_order  INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (group_id) REFERENCES groups_(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS epg_sources (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL DEFAULT '',
            url             TEXT    NOT NULL,
            auto_refresh    INTEGER NOT NULL DEFAULT 1,
            last_refreshed  TEXT    DEFAULT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS schema_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Xtream clients expect numeric identifiers that remain stable between
        -- refreshes. Provider ids and internal UUIDs cannot provide that when
        -- multiple sources are combined, so Teamarr owns a small durable map.
        CREATE TABLE IF NOT EXISTS teamarr_category_ids (
            group_id    TEXT PRIMARY KEY,
            category_id INTEGER NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS teamarr_stream_ids (
            channel_id TEXT PRIMARY KEY,
            stream_id  INTEGER NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS export_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            exported_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            channel_count INTEGER NOT NULL DEFAULT 0,
            group_count   INTEGER NOT NULL DEFAULT 0,
            m3u_size      INTEGER NOT NULL DEFAULT 0,
            xml_size      INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS lineup_snapshots (
            id            TEXT PRIMARY KEY,
            label         TEXT NOT NULL,
            kind          TEXT NOT NULL DEFAULT 'manual',
            payload       BLOB NOT NULL,
            checksum      TEXT NOT NULL,
            channel_count INTEGER NOT NULL DEFAULT 0,
            group_count   INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS action_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            action      TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '',
            snapshot_id TEXT,
            status      TEXT NOT NULL DEFAULT 'completed',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (snapshot_id) REFERENCES lineup_snapshots(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS group_templates (
            id          TEXT    PRIMARY KEY,
            name        TEXT    NOT NULL,
            category    TEXT    NOT NULL DEFAULT '',
            enabled     INTEGER NOT NULL DEFAULT 1,
            pinned      INTEGER NOT NULL DEFAULT 0,
            teamarr     INTEGER NOT NULL DEFAULT 0,
            name_epg    INTEGER NOT NULL DEFAULT 0,
            export_tag  TEXT    NOT NULL DEFAULT '',
            rules_json  TEXT    NOT NULL DEFAULT '[]',
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS channel_health (
            channel_id  TEXT    PRIMARY KEY,
            status_code INTEGER DEFAULT NULL,
            latency_ms  INTEGER DEFAULT NULL,
            error       TEXT    DEFAULT NULL,
            checked_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Persistent export channel numbers. Each channel id keeps the same
        -- tvg-chno across exports even as other channels are added/removed, so
        -- XC players (TiviMate/Televizo) that cache the channel list never
        -- drift their guide onto the wrong channel. See assign_export_chnos().
        CREATE TABLE IF NOT EXISTS chno_map (
            channel_id  TEXT    PRIMARY KEY,
            chno        INTEGER NOT NULL UNIQUE
        );

        -- Provider churn is soft-deleted here for 14 days. A returning stream
        -- reuses its original channel id, player number, group and user edits.
        CREATE TABLE IF NOT EXISTS channel_tombstones (
            channel_id      TEXT PRIMARY KEY,
            source_id       INTEGER NOT NULL,
            stream_id       INTEGER,
            url             TEXT NOT NULL DEFAULT '',
            original_tvg_id TEXT NOT NULL DEFAULT '',
            source_name     TEXT NOT NULL DEFAULT '',
            source_group    TEXT NOT NULL DEFAULT '',
            row_json        TEXT NOT NULL,
            removed_at      TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_ch_source ON channels(source_id);
        CREATE INDEX IF NOT EXISTS idx_ch_group  ON channels(group_id);
        CREATE INDEX IF NOT EXISTS idx_ch_fav    ON channels(favorite);
        CREATE INDEX IF NOT EXISTS idx_ch_name   ON channels(name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_rules_grp ON rules(group_id);
        CREATE INDEX IF NOT EXISTS idx_ch_group_sort ON channels(group_id, sort_order);
        CREATE INDEX IF NOT EXISTS idx_ch_url    ON channels(url);
        CREATE INDEX IF NOT EXISTS idx_ch_enabled ON channels(enabled);
        CREATE INDEX IF NOT EXISTS idx_tomb_source_stream ON channel_tombstones(source_id, stream_id);
        CREATE INDEX IF NOT EXISTS idx_tomb_source_url ON channel_tombstones(source_id, url);
        CREATE INDEX IF NOT EXISTS idx_lineup_snapshots_created ON lineup_snapshots(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_action_history_created ON action_history(created_at DESC);
    """)

    # Migration: add columns to groups_ if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(groups_)").fetchall()]
    if "teamarr" not in cols:
        conn.execute("ALTER TABLE groups_ ADD COLUMN teamarr INTEGER NOT NULL DEFAULT 0")
    if "name_epg" not in cols:
        conn.execute("ALTER TABLE groups_ ADD COLUMN name_epg INTEGER NOT NULL DEFAULT 0")
    if "category" not in cols:
        conn.execute("ALTER TABLE groups_ ADD COLUMN category TEXT NOT NULL DEFAULT ''")
    if "export_tag" not in cols:
        conn.execute("ALTER TABLE groups_ ADD COLUMN export_tag TEXT NOT NULL DEFAULT ''")
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE groups_ ADD COLUMN parent_id TEXT DEFAULT NULL")

    # Sources migrations
    src_cols = [r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
    if "priority" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN priority INTEGER NOT NULL DEFAULT 10")
    if "payment_url" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN payment_url TEXT NOT NULL DEFAULT ''")
    if "last_refreshed" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN last_refreshed TEXT DEFAULT NULL")
    if "auto_refresh" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN auto_refresh INTEGER NOT NULL DEFAULT 0")
    if "refresh_hours" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN refresh_hours INTEGER NOT NULL DEFAULT 24")
    if "last_import_diff" not in src_cols:
        conn.execute("ALTER TABLE sources ADD COLUMN last_import_diff TEXT DEFAULT NULL")

    # Channels migrations
    ch_cols = [r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()]
    if "original_tvg_id" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN original_tvg_id TEXT DEFAULT ''")
        # Backfill: copy current tvg_id as original for non-dummy channels
        conn.execute("UPDATE channels SET original_tvg_id = tvg_id WHERE tvg_id NOT LIKE 'dummy.%'")
    if "channel_number" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN channel_number TEXT DEFAULT ''")
    if "smart_group_id" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN smart_group_id TEXT DEFAULT NULL")
    if "source_order" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN source_order INTEGER NOT NULL DEFAULT 0")
    if "backup_urls" not in ch_cols:
        # Additive failover: JSON array of fallback upstream URLs tried (in
        # order) only when the primary `url` fails during an active restream.
        # Empty string => no backups => primary-only behavior unchanged.
        conn.execute("ALTER TABLE channels ADD COLUMN backup_urls TEXT NOT NULL DEFAULT ''")
    if "placement_locked" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN placement_locked INTEGER NOT NULL DEFAULT 0")
    if "name_locked" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN name_locked INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE channels SET name_locked=1 WHERE name<>source_name AND source_name<>''")

    group_cols = [r[1] for r in conn.execute("PRAGMA table_info(groups_)").fetchall()]
    if "empty_since" not in group_cols:
        conn.execute("ALTER TABLE groups_ ADD COLUMN empty_since TEXT DEFAULT NULL")
    if "archived_at" not in group_cols:
        conn.execute("ALTER TABLE groups_ ADD COLUMN archived_at TEXT DEFAULT NULL")

    for k, v in [
        ("teamarr_enabled", "0"),
        ("teamarr_username", "teamarr"),
        ("teamarr_password", str(uuid.uuid4())[:8]),
        ("teamarr_output", "ts"),
        ("teamarr_base_url", ""),
        ("refresh_interval_minutes", "60"),
    ]:
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)", (k, v))
    _initialize_public_defaults(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','3')"
    )
    conn.commit()
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_GROUP_CATEGORIES = [
    ("Daily", "#7c83ff"), ("24/7", "#a855f7"),
    ("General TV", "#22c55e"), ("Pro Sports", "#f59e0b"),
    ("College Sports", "#f97316"), ("Soccer / International", "#06b6d4"),
    ("PPV / Events", "#ef4444"), ("Radio", "#8b5cf6"),
    ("Archive", "#64748b"),
]


def _group_category(name, archived=False):
    """Return a useful editor category without affecting export order."""
    if archived:
        return "Archive"
    low = (name or "").lower()
    if name in {"Weather", "Antenna", "US | Locals", "US | News", "US | Sports"}:
        return "Daily"
    if low.startswith("24/7") or "ambiance" in low:
        return "24/7"
    if low.startswith("radio"):
        return "Radio"
    if "ppv" in low or any(x in low for x in ("world cup", "masters 20", "wimbledon")):
        return "PPV / Events"
    if any(x in low for x in ("ncaa", "ncaab", "sec+", "acc extra", "big ten", "college")):
        return "College Sports"
    if any(x in low for x in ("soccer", "football", " epl", "mls", "la liga",
                               "ligue", "serie a", "fanatiz")):
        return "Soccer / International"
    if "sports" in low or any(x in low for x in ("nhl", "mlb", "nba", "nfl", "f1")):
        return "Pro Sports"
    return "General TV"


def _initialize_public_defaults(conn):
    """Install neutral editor defaults without changing a user's lineup."""
    done = conn.execute(
        "SELECT value FROM settings WHERE key='public_defaults_v1'"
    ).fetchone()
    if done:
        return

    categories = [
        {"name": name, "color": color, "sort_order": i}
        for i, (name, color) in enumerate(_DEFAULT_GROUP_CATEGORIES)
    ]
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('group_categories',?)",
        (json.dumps(categories, separators=(",", ":")),))

    groups = conn.execute("SELECT id,name,is_custom FROM groups_").fetchall()
    for group in groups:
        count = conn.execute(
            "SELECT COUNT(*) FROM channels WHERE group_id=?", (group["id"],)
        ).fetchone()[0]
        conn.execute(
            "UPDATE groups_ SET category=? WHERE id=? AND COALESCE(category,'')=''",
            (_group_category(group["name"]), group["id"]))

    conn.execute(
        "INSERT INTO settings(key,value) VALUES('public_defaults_v1','1')"
    )


def _stable_numeric_id(table: str, key_column: str, key: str, id_column: str) -> int:
    """Return a persistent positive numeric id for an internal UUID."""
    if table not in {"teamarr_category_ids", "teamarr_stream_ids"}:
        raise ValueError("Unsupported id map")
    with _teamarr_id_lock:
        conn = _conn()
        row = conn.execute(
            f"SELECT {id_column} FROM {table} WHERE {key_column}=?", (key,)
        ).fetchone()
        if row:
            return int(row[0])
        next_id = conn.execute(
            f"SELECT COALESCE(MAX({id_column}),0)+1 FROM {table}"
        ).fetchone()[0]
        conn.execute(
            f"INSERT INTO {table}({key_column},{id_column}) VALUES(?,?)",
            (key, next_id),
        )
        conn.commit()
        return int(next_id)


def get_teamarr_category_id(group_id: str) -> int:
    return _stable_numeric_id("teamarr_category_ids", "group_id", group_id, "category_id")


def get_teamarr_stream_id(channel_id: str) -> int:
    return _stable_numeric_id("teamarr_stream_ids", "channel_id", channel_id, "stream_id")


def get_channel_by_teamarr_stream_id(stream_id: int):
    row = _conn().execute(
        """SELECT c.* FROM channels c
           JOIN teamarr_stream_ids t ON t.channel_id=c.id
           WHERE t.stream_id=?""", (stream_id,)
    ).fetchone()
    return dict(row) if row else None


def maintain_group_archive(days=7):
    """Soft-archive empty provider groups and restore them if they return."""
    conn = _conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """SELECT g.id,g.name,g.enabled,g.is_custom,g.empty_since,g.archived_at,
                  COUNT(c.id) channel_count
           FROM groups_ g LEFT JOIN channels c ON c.group_id=g.id
           GROUP BY g.id"""
    ).fetchall()
    archived = restored = 0
    for row in rows:
        if row["is_custom"]:
            continue
        if row["channel_count"]:
            if row["archived_at"]:
                conn.execute(
                    """UPDATE groups_ SET enabled=1,empty_since=NULL,archived_at=NULL,category=?
                       WHERE id=?""", (_group_category(row["name"]), row["id"]))
                restored += 1
            elif row["empty_since"]:
                conn.execute("UPDATE groups_ SET empty_since=NULL WHERE id=?", (row["id"],))
        elif not row["empty_since"]:
            conn.execute("UPDATE groups_ SET empty_since=datetime('now') WHERE id=?", (row["id"],))
        elif not row["archived_at"] and row["empty_since"] < cutoff:
            conn.execute(
                """UPDATE groups_ SET enabled=0,archived_at=datetime('now'),category='Archive'
                   WHERE id=?""", (row["id"],))
            archived += 1
    conn.commit()
    return {"archived": archived, "restored": restored}

def _rd(row):
    return dict(row) if row else None

def _rl(rows):
    return [dict(r) for r in rows]

def new_id():
    return str(uuid.uuid4())

# Fixed namespace for deriving deterministic smart-group shadow ids.
_SHADOW_NS = uuid.UUID("5f3e9c2a-1d4b-4a6e-9c8f-0b1a2c3d4e5f")

def stable_shadow_id(smart_group_id, channel):
    """Deterministic id for a smart-group shadow channel.

    Shadows are deleted and re-inserted on every group refresh; with a random
    id each time, their chno_map entry (keyed by channel id) is pruned and the
    channel falls back to a positional number — i.e. it drifts. Keying the id
    on the underlying stream's stable identity (its URL, the same key
    sync_channels matches on) makes the same source channel always map to the
    same shadow id, so its chno_map number survives across refreshes."""
    key = channel.get("url") or channel.get("stream_id") or channel.get("name") or ""
    return str(uuid.uuid5(_SHADOW_NS, f"{smart_group_id}|{key}"))

def _sid_filter(source_id, col="source_id"):
    """Build SQL filter clause for source_id (int, list[int], or None)."""
    if source_id is None:
        return "", []
    if isinstance(source_id, (list, tuple)):
        if not source_id:
            return f" AND {col} IN (NULL)", []  # match nothing
        ph = ",".join("?" * len(source_id))
        return f" AND {col} IN ({ph})", list(source_id)
    return f" AND {col}=?", [source_id]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _enrich_src(d):
    if d.get("expires_at"):
        try:
            exp = datetime.fromisoformat(d["expires_at"]).replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            d["days_until_expiry"] = max(0, (exp - now).days)
            d["is_expired"] = exp <= now
        except (ValueError, TypeError):
            d["days_until_expiry"] = d["is_expired"] = None
    else:
        d["days_until_expiry"] = d["is_expired"] = None

def list_sources():
    conn = _conn()
    rows = conn.execute("SELECT * FROM sources ORDER BY is_active DESC, priority ASC, updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r); _enrich_src(d); out.append(d)
    return out

def get_source(sid):
    conn = _conn()
    row = conn.execute("SELECT * FROM sources WHERE id=?", (sid,)).fetchone()
    if not row: return None
    d = dict(row); _enrich_src(d); return d

def get_active_source():
    conn = _conn()
    row = conn.execute("SELECT * FROM sources WHERE is_active=1 LIMIT 1").fetchone()
    if not row: return None
    d = dict(row); _enrich_src(d); return d

def get_active_sources():
    conn = _conn()
    rows = conn.execute("SELECT * FROM sources WHERE is_active=1 ORDER BY updated_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r); _enrich_src(d); out.append(d)
    return out

def get_active_source_id():
    conn = _conn()
    row = conn.execute("SELECT id FROM sources WHERE is_active=1 LIMIT 1").fetchone()
    return row[0] if row else None

def get_active_source_ids():
    conn = _conn()
    rows = conn.execute("SELECT id FROM sources WHERE is_active=1").fetchall()
    return [r[0] for r in rows]

def add_xc(name, server, username, password, output, epg_url,
           expires_at=None, max_connections=None, active_connections=None, status=""):
    conn = _conn()
    cur = conn.execute(
        """INSERT INTO sources (name,type,xc_server,xc_username,xc_password,xc_output,
           epg_url,expires_at,max_connections,active_connections,status)
           VALUES (?,'xc',?,?,?,?,?,?,?,?,?)""",
        (name, server, username, password, output, epg_url,
         expires_at, max_connections, active_connections, status))
    sid = cur.lastrowid; conn.commit()
    return get_source(sid)

def add_m3u(name, m3u_url, epg_url):
    conn = _conn()
    cur = conn.execute("INSERT INTO sources (name,type,m3u_url,epg_url) VALUES (?,'m3u',?,?)",
                       (name, m3u_url, epg_url))
    sid = cur.lastrowid; conn.commit()
    return get_source(sid)

def update_source(sid, **kw):
    conn = _conn()
    sets = ["updated_at=datetime('now')"]
    params = []
    cmap = {"name":"name","server":"xc_server","username":"xc_username",
            "password":"xc_password","output":"xc_output","m3u_url":"m3u_url",
            "epg_url":"epg_url","payment_url":"payment_url","expires_at":"expires_at",
            "max_connections":"max_connections","active_connections":"active_connections",
            "status":"status","auto_refresh":"auto_refresh",
            "refresh_hours":"refresh_hours","last_refreshed":"last_refreshed",
            "priority":"priority","last_import_diff":"last_import_diff"}
    for k, v in kw.items():
        if k in cmap:
            sets.append(f"{cmap[k]}=?"); params.append(v)
    params.append(sid)
    conn.execute(f"UPDATE sources SET {','.join(sets)} WHERE id=?", params)
    conn.commit()
    return get_source(sid)

def activate_source(sid):
    """Toggle a source's active state (does NOT deactivate others)."""
    conn = _conn()
    current = conn.execute("SELECT is_active FROM sources WHERE id=?", (sid,)).fetchone()
    new_val = 0 if (current and current[0]) else 1
    conn.execute("UPDATE sources SET is_active=? WHERE id=?", (new_val, sid))
    conn.commit()
    return new_val

def force_activate_source(sid):
    """Set a source to active=1 without toggling (for imports)."""
    conn = _conn()
    conn.execute("UPDATE sources SET is_active=1 WHERE id=?", (sid,))
    conn.commit()

def deactivate_all():
    conn = _conn()
    conn.execute("UPDATE sources SET is_active=0")
    conn.commit()

def delete_source(sid):
    conn = _conn()
    conn.execute("DELETE FROM channels WHERE source_id=?", (sid,))
    conn.execute("DELETE FROM sources WHERE id=?", (sid,))
    conn.commit()


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def insert_channels(channels):
    if not channels: return
    conn = _conn()
    conn.executemany(
        """INSERT OR REPLACE INTO channels
           (id,source_id,stream_id,name,source_name,source_group,
            group_id,tvg_id,original_tvg_id,tvg_name,logo,url,enabled,favorite,sort_order,channel_number)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(c["id"], c["source_id"], c.get("stream_id"),
          c["name"], c.get("source_name",""), c.get("source_group","Ungrouped"),
          c.get("group_id"), c.get("tvg_id",""), c.get("tvg_id",""),
          c.get("tvg_name",""),
          c.get("logo",""), c.get("url",""),
          1 if c.get("enabled",True) else 0,
          1 if c.get("favorite",False) else 0,
          c.get("sort_order",0),
          c.get("channel_number","")) for c in channels])
    conn.commit()

def clear_source_channels(sid):
    conn = _conn()
    conn.execute("DELETE FROM channels WHERE source_id=?", (sid,))
    # A reset is intentional, unlike normal provider churn. Do not revive the
    # old lineup after the user explicitly reset this source.
    conn.execute("DELETE FROM channel_tombstones WHERE source_id=?", (sid,))
    conn.commit()

def sync_channels(source_id, fresh_channels):
    """Sync channels from a fresh import against existing DB channels for this source.

    Uses provider stream_id first (XC) and URL second as stable identifiers.
    - New channels (URL not in DB): inserted
    - Existing channels: source metadata updated (source_name, source_group, logo,
      channel_number, tvg_name) but user edits preserved (name, tvg_id, enabled,
      favorite, group_id, sort_order)
    - Removed channels (in DB but not in fresh): deleted
    - Smart group shadow channels are excluded from matching so originals are restored.

    Returns dict with counts: added, updated, removed.
    """
    conn = _conn()
    existing = conn.execute(
        "SELECT * FROM channels WHERE source_id=? AND (smart_group_id IS NULL OR smart_group_id = '')",
        (source_id,)).fetchall()
    existing_by_url = {}
    existing_by_stream = {}
    for row in existing:
        d = dict(row)
        url = d.get("url") or ""
        if url:
            existing_by_url[url] = d
        if d.get("stream_id") is not None:
            existing_by_stream[d["stream_id"]] = d

    added = 0
    updated = 0
    revived = 0
    matched_existing_ids = set()

    # Update existing + insert new
    for fch in fresh_channels:
        url = fch.get("url") or ""
        old = (existing_by_stream.get(fch.get("stream_id"))
               if fch.get("stream_id") is not None else None)
        if old is None and url:
            old = existing_by_url.get(url)
        if old is not None and old["id"] in matched_existing_ids:
            old = None
        if old is not None:
            # Existing channel: update source metadata only
            matched_existing_ids.add(old["id"])
            display_name = old.get("name") if old.get("name_locked") else fch.get("source_name", "")
            conn.execute(
                """UPDATE channels SET name=?, source_name=?, source_group=?, logo=?,
                   channel_number=?, tvg_name=?, stream_id=?, source_order=?, url=?
                   WHERE id=?""",
                (display_name or fch.get("source_name", "") or "Unnamed", fch.get("source_name", ""),
                 fch.get("source_group", "Ungrouped"),
                 fch.get("logo", ""), fch.get("channel_number", ""),
                 fch.get("tvg_name", ""), fch.get("stream_id"),
                 fch.get("source_order", 0), url,
                 old["id"]))
            # Update original_tvg_id if source changed it
            new_tvg = fch.get("tvg_id", "")
            if new_tvg and new_tvg != old.get("original_tvg_id", ""):
                conn.execute("UPDATE channels SET original_tvg_id=? WHERE id=?",
                             (new_tvg, old["id"]))
                # Also update tvg_id if user hasn't customized it (still matches old original)
                if old.get("tvg_id", "") == old.get("original_tvg_id", ""):
                    conn.execute("UPDATE channels SET tvg_id=? WHERE id=?",
                                 (new_tvg, old["id"]))
            updated += 1
        else:
            # Revive a recently missing channel before treating it as new.
            tomb = None
            if fch.get("stream_id") is not None:
                tomb = conn.execute(
                    """SELECT * FROM channel_tombstones
                       WHERE source_id=? AND stream_id=? ORDER BY removed_at DESC LIMIT 1""",
                    (source_id, fch.get("stream_id"))).fetchone()
            if tomb is None and url:
                tomb = conn.execute(
                    """SELECT * FROM channel_tombstones
                       WHERE source_id=? AND url=? ORDER BY removed_at DESC LIMIT 1""",
                    (source_id, url)).fetchone()
            if tomb is None and (fch.get("tvg_id") or ""):
                tomb = conn.execute(
                    """SELECT * FROM channel_tombstones
                       WHERE source_id=? AND original_tvg_id=? AND source_name=?
                       ORDER BY removed_at DESC LIMIT 1""",
                    (source_id, fch.get("tvg_id", ""), fch.get("source_name", ""))).fetchone()

            if tomb is not None:
                restored = json.loads(tomb["row_json"])
                group_id = restored.get("group_id")
                if group_id and not conn.execute("SELECT 1 FROM groups_ WHERE id=?", (group_id,)).fetchone():
                    group_id = None
                restored.update(
                    source_id=source_id, stream_id=fch.get("stream_id"),
                    source_name=fch.get("source_name", ""),
                    source_group=fch.get("source_group", "Ungrouped"),
                    logo=fch.get("logo", ""), channel_number=fch.get("channel_number", ""),
                    tvg_name=fch.get("tvg_name", ""), source_order=fch.get("source_order", 0),
                    url=url, group_id=group_id,
                )
                if not restored.get("name_locked"):
                    restored["name"] = fch.get("source_name") or fch.get("name") or "Unnamed"
                new_tvg = fch.get("tvg_id", "")
                if restored.get("tvg_id", "") == restored.get("original_tvg_id", ""):
                    restored["tvg_id"] = new_tvg
                restored["original_tvg_id"] = new_tvg
                cols = [r[1] for r in conn.execute("PRAGMA table_info(channels)")]
                values = [restored.get(col) for col in cols]
                conn.execute(
                    f"INSERT INTO channels ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                    values)
                conn.execute("DELETE FROM channel_tombstones WHERE channel_id=?", (tomb["channel_id"],))
                revived += 1
            else:
                # Brand-new channel.
                conn.execute(
                    """INSERT INTO channels
                       (id,source_id,stream_id,name,source_name,source_group,
                        group_id,tvg_id,original_tvg_id,tvg_name,logo,url,
                        enabled,favorite,sort_order,channel_number,source_order)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (fch["id"], source_id, fch.get("stream_id"),
                     fch["name"], fch.get("source_name", ""), fch.get("source_group", "Ungrouped"),
                     fch.get("group_id"), fch.get("tvg_id", ""), fch.get("tvg_id", ""),
                     fch.get("tvg_name", ""), fch.get("logo", ""), url,
                     1 if fch.get("enabled", True) else 0, 0, fch.get("sort_order", 0),
                     fch.get("channel_number", ""), fch.get("source_order", 0)))
                added += 1

    # Remove channels no longer in the source
    removed = 0
    for old_row in existing:
        old = dict(old_row)
        if old["id"] not in matched_existing_ids:
            conn.execute(
                """INSERT OR REPLACE INTO channel_tombstones
                   (channel_id,source_id,stream_id,url,original_tvg_id,source_name,
                    source_group,row_json,removed_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                (old["id"], source_id, old.get("stream_id"), old.get("url") or "",
                 old.get("original_tvg_id") or "", old.get("source_name") or "",
                 old.get("source_group") or "", json.dumps(old, separators=(",", ":"))))
            conn.execute("DELETE FROM channels WHERE id=?", (old["id"],))
            removed += 1

    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("DELETE FROM channel_tombstones WHERE removed_at < ?", (cutoff,))

    conn.commit()
    return {"added": added, "updated": updated, "removed": removed, "revived": revived}

def assign_export_chnos(ordered_ids):
    """Return a STABLE {channel_id: chno} mapping for an export.

    Channel numbers are persistent per channel id, not positional. A channel
    keeps its tvg-chno across exports even when other channels come and go —
    which is what stops TiviMate/Televizo's cached guide from sliding onto the
    wrong channel when the lineup churns (e.g. the daily sports-event
    channels). Behaviour:
      - assignments for channels that no longer exist are pruned, freeing their
        numbers for reuse (keeps the transient event-channel band compact);
      - channels already mapped keep their number;
      - brand-new ids get the lowest free positive integer, allocated in the
        given (export) order.
    On a virgin map this reproduces the old 1..N positional numbering exactly
    (seeded from the current order), so switching to it causes no one-time
    renumber for channels already present.
    """
    conn = _conn()
    # Free numbers held by channels that have been deleted entirely.
    conn.execute(
        """DELETE FROM chno_map
           WHERE channel_id NOT IN (SELECT id FROM channels)
             AND channel_id NOT IN (SELECT channel_id FROM channel_tombstones)""")
    cur = {r["channel_id"]: r["chno"]
           for r in conn.execute("SELECT channel_id, chno FROM chno_map")}
    used = set(cur.values())
    new = []
    nxt = 1
    for cid in ordered_ids:
        if cid in cur:
            continue
        while nxt in used:
            nxt += 1
        cur[cid] = nxt
        used.add(nxt)
        new.append((cid, nxt))
        nxt += 1
    if new:
        conn.executemany("INSERT OR REPLACE INTO chno_map(channel_id, chno) VALUES(?,?)", new)
    conn.commit()
    return cur


# Auto-renumber (from a user reorder/favorite action) is skipped above this
# group size. Finding a free CONTIGUOUS block gets harder as it grows, and on
# a system with thousands of channels across ~500+ groups, a big group with no
# nearby gap gets flung to wherever the first big-enough gap happens to be —
# which can be far from its neighborhood (e.g. a 637-channel group jumping
# from the 500s to the 9500s, sorting it last in anything ordered by channel
# number — see the 2026-07-06 24/7 Kids incident). Below this size a nearby
# gap is reliably available and the jump, if any, is small. The manual
# /api/groups/{gid}/renumber-block endpoint is NOT capped — it's a deliberate
# one-off action, not something that should silently fire on every reorder.
MAX_AUTO_RENUMBER_GROUP_SIZE = 50


def renumber_group_block(ordered_cids, anchor_hint=None):
    """Give a deliberately-ordered group a CONTIGUOUS ascending block of stable
    chnos, in the supplied display order, so the group shows in that order even
    in players that sort purely by channel number (the stable-chno scheme
    otherwise freezes numbers at first-seen order, which can scatter a reordered
    group). Finds the lowest start S >= anchor where [S, S+N) are all free —
    ignoring the group's own current numbers, which are vacated — then assigns
    S..S+N-1 in order. Written to chno_map like every other assignment, so it
    sticks. Returns {channel_id: chno}.

    anchor defaults to the group's own current minimum chno (so it grows
    outward from where it already lives instead of always restarting the
    search at 1) — without this, a large group with no nearby gap can get
    flung across the whole number space to wherever the first big-enough gap
    happens to be (e.g. 222 channels jumping from the 500s to the 9500s,
    which then sorts the entire group last in anything ordered by channel
    number). Pass anchor_hint explicitly for a one-off corrective renumber
    when the group's own stored numbers are themselves the bad state.
    """
    ordered_cids = [c for c in ordered_cids if c]
    if not ordered_cids:
        return {}
    conn = _conn()
    n = len(ordered_cids)
    own = set(ordered_cids)
    cur = {r["channel_id"]: r["chno"]
           for r in conn.execute("SELECT channel_id, chno FROM chno_map")}
    occupied = {c for cid, c in cur.items() if cid not in own}
    if anchor_hint is not None:
        anchor = anchor_hint
    else:
        existing = [v for cid, v in cur.items() if cid in own]
        anchor = min(existing) if existing else 1
    s = max(1, anchor)
    while any((s + i) in occupied for i in range(n)):
        s += 1
    rows = [(cid, s + i) for i, cid in enumerate(ordered_cids)]
    conn.executemany("INSERT OR REPLACE INTO chno_map(channel_id, chno) VALUES(?,?)", rows)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('epg_chno_rebaseline_pending','1')"
    )
    conn.commit()
    return dict(rows)


def reorder_group_chnos_in_place(ordered_cids):
    """Make stable channel numbers follow a group's display order safely.

    Reuse the group's current sorted set of numbers instead of searching for a
    new contiguous block.  This works for large groups without relocating the
    group into a distant gap in the global lineup.  Temporary negative values
    avoid UNIQUE(chno) collisions while numbers are swapped.
    """
    ordered_cids = [c for c in ordered_cids if c]
    if not ordered_cids:
        return {}
    conn = _conn()
    # Ensure every channel has a stable number before redistributing the set.
    assign_export_chnos(ordered_cids)
    placeholders = ",".join("?" * len(ordered_cids))
    rows = conn.execute(
        f"SELECT channel_id, chno FROM chno_map WHERE channel_id IN ({placeholders})",
        ordered_cids,
    ).fetchall()
    by_id = {r["channel_id"]: r["chno"] for r in rows}
    numbers = sorted(by_id[cid] for cid in ordered_cids if cid in by_id)
    if len(numbers) != len(ordered_cids):
        raise RuntimeError("could not allocate stable channel numbers for group reorder")

    floor = conn.execute("SELECT COALESCE(MIN(chno), 0) FROM chno_map").fetchone()[0]
    temp_start = min(-1, floor - len(ordered_cids) - 1)
    try:
        for i, cid in enumerate(ordered_cids):
            conn.execute("UPDATE chno_map SET chno=? WHERE channel_id=?", (temp_start - i, cid))
        for cid, chno in zip(ordered_cids, numbers):
            conn.execute("UPDATE chno_map SET chno=? WHERE channel_id=?", (chno, cid))
        conn.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES('epg_chno_rebaseline_pending','1')"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(zip(ordered_cids, numbers))


def renumber_all_in_order(ordered_ids):
    """Wipe chno_map entirely and reassign sequential 1..N stable channel
    numbers in EXACTLY the given order (group order, then channel order
    within each group). Unlike assign_export_chnos/renumber_group_block,
    this does NOT try to preserve any existing number — it's a deliberate,
    explicit, one-time GLOBAL renumber for when the persistent-number
    layout has drifted so far from the current group/sort order that
    players sorting by channel number (Dispatcharr in provider-numbering
    mode, HDHR, etc.) show groups in the wrong sequence everywhere, not
    just one group. Every channel's number can change, so any XC client
    (TiviMate/Televizo) with a cached playlist needs a one-time re-add
    afterward, same as the original stable-chno rollout."""
    ordered_ids = [c for c in ordered_ids if c]
    conn = _conn()
    conn.execute("DELETE FROM chno_map")
    rows = [(cid, i + 1) for i, cid in enumerate(ordered_ids)]
    if rows:
        conn.executemany("INSERT OR REPLACE INTO chno_map(channel_id, chno) VALUES(?,?)", rows)
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('epg_chno_rebaseline_pending','1')"
    )
    conn.commit()
    return dict(rows)


def get_channel(cid):
    conn = _conn()
    row = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    return _rd(row)

def update_channel(cid, **kw):
    conn = _conn()
    sets, params = [], []
    ok = {"name","tvg_id","tvg_name","enabled","favorite","group_id","sort_order",
          "logo","url","backup_urls","placement_locked","name_locked"}
    for k, v in kw.items():
        if k in ok:
            sets.append(f"{k}=?"); params.append(v)
    if not sets:
        return get_channel(cid)
    params.append(cid)
    conn.execute(f"UPDATE channels SET {','.join(sets)} WHERE id=?", params)
    conn.commit()
    return get_channel(cid)

def clear_dummy_tvg_ids():
    conn = _conn()
    conn.execute("UPDATE channels SET tvg_id = '' WHERE tvg_id LIKE 'dummy-%' OR tvg_id LIKE 'dummy.%'")
    conn.commit()

def move_channel(cid, to_gid):
    conn = _conn()
    mx = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM channels WHERE group_id=?",
                       (to_gid,)).fetchone()[0]
    conn.execute(
        "UPDATE channels SET group_id=?, sort_order=?, placement_locked=1 WHERE id=?",
        (to_gid, mx, cid))
    conn.commit()

def get_group_max_sort_order(gid):
    conn = _conn()
    mx = conn.execute("SELECT COALESCE(MAX(sort_order),-1) FROM channels WHERE group_id=?",
                       (gid,)).fetchone()[0]
    return mx

def _tombstone_channel(conn, channel):
    """Retain a recoverable private row before an intentional deletion."""
    row = dict(channel)
    conn.execute(
        """INSERT OR REPLACE INTO channel_tombstones
           (channel_id,source_id,stream_id,url,original_tvg_id,source_name,
            source_group,row_json,removed_at)
           VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
        (row["id"], row["source_id"], row.get("stream_id"), row.get("url") or "",
         row.get("original_tvg_id") or "", row.get("source_name") or "",
         row.get("source_group") or "", json.dumps(row, separators=(",", ":"))),
    )


def delete_channel(cid):
    conn = _conn()
    row = conn.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
    if row:
        _tombstone_channel(conn, row)
    conn.execute("DELETE FROM channels WHERE id=?", (cid,))
    conn.commit()

def delete_channels_bulk(cids):
    if not cids: return 0
    conn = _conn()
    ph = ",".join("?"*len(cids))
    for row in conn.execute(f"SELECT * FROM channels WHERE id IN ({ph})", cids).fetchall():
        _tombstone_channel(conn, row)
    conn.execute(f"DELETE FROM channels WHERE id IN ({ph})", cids)
    conn.commit()
    return len(cids)

def reorder_group_channels(gid, ordered_cids, mark_custom=True):
    """Set sort_order for channels in a group based on the provided ID list order.

    A MANUAL reorder/sort marks the group is_custom=1 so apply_source_order_to_groups()
    won't later clobber the arrangement with the provider's channel order on the next
    source refresh. Automatic callers (e.g. smart-group city promotion) pass
    mark_custom=False so they don't freeze dynamic groups. Source-priority sorting
    (sort-by-source) still works — it just STICKS afterwards instead of being re-overridden.

    A mark_custom=True reorder also redistributes the group's existing stable
    chnos to match the new order. Players like Dispatcharr sort/display by chno,
    not sort_order, so both representations must agree."""
    if not ordered_cids: return
    conn = _conn()
    for i, cid in enumerate(ordered_cids):
        conn.execute("UPDATE channels SET sort_order=? WHERE id=? AND group_id=?", (i, cid, gid))
    if mark_custom:
        conn.execute("UPDATE groups_ SET is_custom=1 WHERE id=?", (gid,))
    conn.commit()
    if mark_custom:
        reorder_group_chnos_in_place(ordered_cids)

def apply_source_order_to_groups():
    """Compatibility hook: preserve the established station order.

    Refreshes used to rewrite every non-custom group's ``sort_order`` from the
    provider feed. Providers routinely reshuffle their feeds, so that made a
    daily lineup drift even when every stream was still present. New stations
    are appended by the importer; only an explicit user sort/reorder changes
    existing positions now.
    """
    return 0

def promote_favorites_in_group(gid):
    """Re-sort a single group so favorited channels come first, preserving relative order.

    Only called from explicit user favorite-toggle actions (single-channel PATCH,
    bulk-favorite) — not from the automatic per-refresh apply_source_order_to_groups
    pass — so, like reorder_group_channels, it also reflows stable chnos to match
    the new order. Without this, favoriting a channel to the top changes its
    display position but never its player-visible channel number (same bug as
    drag-reorder before stable-number redistribution was wired in there)."""
    conn = _conn()
    rows = conn.execute(
        """SELECT id FROM channels
           WHERE group_id=? AND (smart_group_id IS NULL OR smart_group_id='')
           ORDER BY favorite DESC, sort_order ASC""",
        (gid,)).fetchall()
    if not rows:
        return
    for i, r in enumerate(rows):
        conn.execute("UPDATE channels SET sort_order=? WHERE id=?", (i, r["id"]))
    conn.commit()
    reorder_group_chnos_in_place([r["id"] for r in rows])

# ── SiriusXM channel number mapping (official XM# from siriusxm.com) ──
_SXM_MAP = {
    # Pop
    "SiriusXM Hits 1": 2, "Unwell Music": 3, "Life with John Mayer": 4,
    "The Pulse": 5, "PopRocks": 6, "70s on 7": 7, "80s on 8": 8,
    "90s on 9": 9, "Pop2K": 10, "The 10s Spot": 11,
    "The Kelly Clarkson Connection": 12, "Kelly Clarkson Connection": 12,
    "Pitbull's Globalization": 13, "Yacht Rock Radio": 15,
    "The Blend": 16, "The Coffee House": 17, "50s Gold": 72,
    "60s Gold": 73, "Elvis Radio": 76, "Road Trip Radio": 301,
    "Andy Cohen's Kiki Lounge": 302, "Mosaic": 305,
    "Pop Top 500": 550, "80s on 8 Top 500": 551, "90s on 9 Top 500": 552,
    "Pandora Now": 703, "SoulCycle Radio": 704, "SiriusXM K-Pop": 705,
    "SiriusXM Love": 708, "SiriusXO": 709,
    "Billboard Top 500": 554, "Billboard 2025 #1s": 555,
    # Rock
    "The Bridge": 14, "The Beatles Channel": 18,
    "Bob Marley's Tuff Gong": 19, "Bob Marley's Tuff Gong Radio": 19,
    "E Street Radio": 20, "Underground Garage": 21,
    "Pearl Jam Radio": 22, "Grateful Dead": 23, "The Grateful Dead Channel": 23,
    "Radio Margaritaville": 24, "Classic Rewind": 25,
    "Classic Vinyl": 26, "Alt2K": 27, "Alt 2K": 27,
    "The Spectrum": 28, "Phish Radio": 29,
    "Dave Matthews Band Radio": 30, "Tom Petty Radio": 31,
    "Petty's Buried Treasure": 711, "U2 X-Radio": 32,
    "1st Wave": 33, "Lithium": 34, "SiriusXMU": 35,
    "Alt Nation": 36, "Octane": 37,
    "Ozzy's Boneyard": 38, "Hair Nation": 39,
    "Liquid Metal": 40, "SiriusXM Turbo": 41,
    "Maximum Metallica": 42, "Deep Tracks": 308,
    "Jam On 309": 309, "Jam On": 309, "Bon Jovi Radio": 312,
    "FACTION PUNK": 314, "Faction Punk": 314,
    "Red Hot Chili Peppers": 315,
    "Classic Rock Top 1000": 553, "The Loft": 710,
    "Marky Ramone's Punk Rock": 712, "RockBar": 713,
    "Classic Rock Party": 715,
    "Stevie's Coolest Songs": 721,
    # Urban & Caribbean
    "Rock The Bells Radio": 43, "Rock the Bells Radio": 43,
    "Hip-Hop Nation": 44, "Shade 45": 45,
    "The Heat": 46, "Heart & Soul": 47,
    "The Flow": 48, "Flex2K": 49,
    "SiriusXM FLY": 50, "The Groove": 51,
    "Smokey's Soul Town": 74, "Smokey's SoulTown": 74,
    "SiriusXM Silk 330": 330, "SiriusXM Silk": 330,
    "Shaggy Boombastic Radio": 332, "Shaggy's Boombastic Radio": 332,
    "Grown Folk JAMZ": 362, "Grown Folks JAMZ": 362,
    "Hip-Hop Chronicles": 556, "Hip-hop Chronicles": 556,
    "Sway's Universe": 720,
    "RTB - Mixdown": 723, "Rock the Bells Mixdown": 723,
    "SoundCloud Radio": 724,
    # Dance/Electronic
    "BPM": 52, "Diplo's Revolution": 53,
    "Studio 54 Radio": 54, "SiriusXM Chill": 55,
    "Radio Monaco": 340, "Utopia": 341,
    "Steve Aoki's Remix Radio": 735, "A State of Armin": 736,
    "One World Radio": 738,
    # Country
    "The Highway": 56, "Y2Kountry": 57,
    "Prime Country": 58, "No Shoes Radio": 59,
    "Carrie's Country": 60,
    "Willie's Roadhouse": 61, "Outlaw Country": 62,
    "Chris Stapleton Radio": 63, "Morgan Wallen Radio": 64,
    "Bluegrass Junction": 77,
    "Bakersfield Beat": 349, "Dwight Yoakam & the Bakersfield Beat": 349,
    "Red White & Booze": 350, "Red, White & Booze": 350,
    "Country Top 1000": 558,
    "Savior Sunday Daily": 739, "Outsiders Radio": 740,
    "The Village": 741,
    # Christian
    "Kirk Franklin's Praise": 64, "The Message": 65,
    "Message Worship": 68, "Bill Gaither's enLighten": 150,
    # Jazz, Standards & Classical
    "Watercolors": 66, "Real Jazz": 67,
    "On Broadway": 69, "Siriusly Sinatra": 70,
    "40s Junction": 71, "BB King's Bluesville": 75, "B.B. King's Bluesville": 75,
    "Symphony Hall": 78, "Escape": 149,
    "Met Opera Radio": 744, "SiriusXM Pops": 745, "Spa": 746,
    # Kids & Family
    "Disney Hits": 133, "Kids Place": 134, "Kids Place Live": 134,
    "KIDZ BOP Radio": 135, "Kidz Bop Radio": 135,
    "Moonbug Radio": 136, "CoComelon & Friends": 136,
    "Disney Jr. Radio": 702,
    # Sports
    "ESPN Radio": 80, "ESPN Xtra": 81,
    "Mad Dog Sports Radio": 82,
    "FOX Sports on SiriusXM": 83, "Fox Sports on SiriusXM": 83,
    "College Sports Radio": 84, "SiriusXM College Sports Radio": 84,
    "NBC Sports Radio": 85, "SiriusXM NBA Radio": 86,
    "Fantasy Sports Radio": 87, "SiriusXM Fantasy Sports Radio": 87,
    "SiriusXM NFL Radio": 88, "MLB Network Radio": 89,
    "SiriusXM NASCAR Radio": 90, "NHL Network Radio": 91, "SiriusXM NHL Network Radio": 91,
    "SiriusXM PGA TOUR Radio": 92, "SiriusXM PGA Tour Radio": 92,
    "Pro Wrestling Nation24/7": 156, "Pro Wrestling Nation 24/7": 156,
    "SiriusXM FC": 157, "SiriusXM INDYCAR Nation": 218,
    "ESPN Podcasts": 370, "SiriusXM ACC Radio": 371,
    "SiriusXM Big Ten Radio": 372, "SiriusXM SEC Radio": 374,
    "Infinity Sports Network": 375, "SiriusXM Scoreboard": 172,
    # Comedy
    "Netflix Is A Joke Radio": 93, "Netflix is a Joke Radio": 93,
    "Comedy Greats": 94, "Comedy Central Radio": 95,
    "Kevin Hart's LOL Radio": 96, "Comedy Roundup": 97,
    "Pure Comedy": 98, "Raw Comedy": 99,
    "She's So Funny": 771, "Comedy Classics": 772,
    # Entertainment & Talk
    "Howard 100": 100, "Howard 101": 101,
    "Radio Andy": 102, "Faction Talk": 103,
    "Conan O'Brien Radio": 104, "Dateline 24/7": 107,
    "TODAY Show Radio": 108, "Stars": 109,
    "Doctor Radio": 110, "Triumph": 123,
    "Business Radio": 132, "Road Dog Trucking": 146,
    "Radio Classics": 148, "SiriusXM App Originals": 781,
    "SmartLess Radio": 787, "Crime Junkie Radio": 788,
    "The Jeff Lewis Channel": 789, "Entertainment Now": 790,
    "Freakonomics Radio": 791, "Ramsey Network": 792, "Law&Crime": 793,
    # News
    "The Megyn Kelly Channel": 111, "CNBC": 112,
    "FOX Business": 113, "Fox Business": 113,
    "FOX News Channel": 114, "Fox News Channel": 114,
    "FOX News Headlines 24/7": 115, "Fox News Headlines 24/7": 115,
    "CNN": 116, "MS NOW": 118,
    "PRX Remix": 119, "BBC World Service": 120,
    "Bloomberg Radio": 121, "NPR Now": 122,
    "POTUS Politics": 124, "SiriusXM Patriot": 125,
    "SiriusXM Urban View": 126, "SiriusXM Progress": 127,
    "RURAL Radio": 147, "Rural Radio": 147,
    "C-SPAN Radio": 455, "NBC News NOW": 798,
    # Religion
    "Joel Osteen Radio": 128, "The Catholic Channel": 129,
    "EWTN Radio": 130, "Family Talk": 131,
    "The Billy Graham Channel": 460,
    # More
    "Holy Culture Radio": 140, "HUR Voices": 141, "HBCU": 142,
    "BYUradio": 143, "BYU Radio": 143, "SLAM Radio": 145,
    # Latin
    "Hits Uno": 151, "Caliente": 152,
    "En Vivo": 154, "Latin Vault": 155,
    "Chucho's Cuba & Beyond": 760, "Celia Cruz AZÚCAR!": 761,
    "Caricia": 762, "Latidos": 764,
    "Flow Nación": 765, "Luna": 766,
    "Rumbón": 767, "La Kueva": 768,
    "Soccer en Español": 769,
    # French-language / Canadian / International
    "Korea Today": 144, "Attitude Franco": 163,
    "Mixtape: North": 164, "The Indigiverse": 165,
    "Racines Musicales": 166, "Canada Talks": 167,
    "SiriusXM Comedy Club": 168, "CBC Radio One": 169,
    "ICI Première": 170, "SiriusXM Scoreboard": 172,
    "The Verge": 173, "Influence Franco": 174,
    "North Americana": 359, "Poplandia": 754,
    "Iceberg": 758, "Les Tubes Franco": 759,
    "SiriusXM Dhamaka": 796,
    # Seasonal
    "Holiday Traditions": 602, "Holiday Pops": 622,
    "Hallmark Radio": 105, "Navidad": 626,
    "Radio Hanukkah": 638, "Holidays with AnneMurray": 639,
    "Noël Incontournable": 640, "Mannheim Steamroller": 620,
    "Smokey's HolidaySoulTown": 612,
}

def _sxm_sort_key(name):
    """Extract SiriusXM channel number from channel name for sorting.

    Handles:
    - 'Radio: <channel name>' prefix stripping
    - Direct lookup in _SXM_MAP
    - Numeric suffix extraction (e.g. 'Sports 963', 'Big 12 952')
    - Sports team names sort after all numbered channels
    """
    import re
    # Strip 'Radio: ' prefix
    clean = re.sub(r'^Radio:\s*', '', name).strip()
    # Direct lookup
    if clean in _SXM_MAP:
        return (_SXM_MAP[clean], clean)
    # Try extracting trailing number (Sports 963, Big 12 952, etc.)
    m = re.search(r'(\d{3,4})$', clean)
    if m:
        return (int(m.group(1)), clean)
    # MLB en Español 870, NFL en Español 832
    m = re.search(r'(\d+)$', clean)
    if m:
        return (int(m.group(1)), clean)
    # Limited Edition channels — sort after numbered content
    m = re.match(r'Limited Edition\s*(\d+)', clean)
    if m:
        return (900 + int(m.group(1)), clean)
    # Unknown → sort after everything, alphabetically
    return (9999, clean)

def sort_sxm_group(gid=None):
    """Sort a group by official SiriusXM channel numbers.
    If gid is None, auto-detect 'Radio | Sirius XM' group."""
    conn = _conn()
    if not gid:
        row = conn.execute("SELECT id FROM groups_ WHERE name=?", ("Radio | Sirius XM",)).fetchone()
        if not row:
            return 0
        gid = row["id"]
    channels = conn.execute(
        """SELECT id, name, favorite FROM channels
           WHERE group_id=? AND (smart_group_id IS NULL OR smart_group_id='')""",
        (gid,)).fetchall()
    if not channels:
        return 0
    sorted_chs = sorted(channels, key=lambda ch: _sxm_sort_key(ch["name"]))
    for i, ch in enumerate(sorted_chs):
        conn.execute("UPDATE channels SET sort_order=? WHERE id=?", (i, ch["id"]))
    conn.commit()
    return len(sorted_chs)

def bulk_move_channels(cids, to_gid):
    if not cids: return 0
    conn = _conn()
    base = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM channels WHERE group_id=?",
                        (to_gid,)).fetchone()[0]
    for i, cid in enumerate(cids):
        conn.execute(
            "UPDATE channels SET group_id=?, sort_order=?, placement_locked=1 WHERE id=?",
            (to_gid, base+i, cid))
    conn.commit()
    return len(cids)

def bulk_copy_channels(cids, to_gid):
    """Duplicate channels into target group, leaving originals in place."""
    if not cids: return 0
    conn = _conn()
    base = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM channels WHERE group_id=?",
                        (to_gid,)).fetchone()[0]
    ph = ",".join("?" * len(cids))
    rows = conn.execute(f"SELECT * FROM channels WHERE id IN ({ph})", cids).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM channels LIMIT 0").description]
    for i, row in enumerate(rows):
        d = dict(zip(cols, row))
        d["id"] = new_id()
        d["group_id"] = to_gid
        d["sort_order"] = base + i
        d["placement_locked"] = 1
        placeholders = ",".join("?" * len(cols))
        conn.execute(f"INSERT INTO channels ({','.join(cols)}) VALUES ({placeholders})",
                     [d[c] for c in cols])
    conn.commit()
    return len(rows)

def bulk_toggle_channels(cids, enabled):
    if not cids: return 0
    conn = _conn()
    ph = ",".join("?"*len(cids))
    conn.execute(f"UPDATE channels SET enabled=? WHERE id IN ({ph})", [1 if enabled else 0]+cids)
    conn.commit()
    return len(cids)

def bulk_favorite_channels(cids, fav):
    if not cids: return 0
    conn = _conn()
    ph = ",".join("?"*len(cids))
    conn.execute(f"UPDATE channels SET favorite=? WHERE id IN ({ph})", [1 if fav else 0]+cids)
    conn.commit()
    return len(cids)


def bulk_lock_channels(cids, locked):
    """Protect or release channel placement for a selected station set."""
    if not cids:
        return 0
    conn = _conn()
    placeholders = ",".join("?" * len(cids))
    conn.execute(
        f"UPDATE channels SET placement_locked=? WHERE id IN ({placeholders})",
        [1 if locked else 0] + cids,
    )
    conn.commit()
    return len(cids)

def get_group_channels(gid, offset=0, limit=100, query="", source_id=None, team_keywords=None):
    conn = _conn()
    w = "WHERE c.group_id=?"
    p = [gid]
    sf, sp = _sid_filter(source_id, col="c.source_id")
    w += sf; p += sp
    if query:
        w += " AND (c.name LIKE ? OR c.tvg_id LIKE ? OR c.source_group LIKE ?)"
        q = f"%{query}%"; p += [q, q, q]
    total = conn.execute(f"SELECT COUNT(*) FROM channels c {w}", p).fetchone()[0]

    rows = conn.execute(
        f"""SELECT c.* FROM channels c
            {w} ORDER BY c.sort_order ASC LIMIT ? OFFSET ?""",
        p + [limit, offset]).fetchall()
    return _rl(rows), total

def search_channels(query="", limit=60, offset=0, favorites_only=False, enabled_only=False,
                    no_epg=False, group_id=None, source_id=None):
    conn = _conn()
    conds, params = [], []
    if query:
        conds.append("(c.name LIKE ? OR c.tvg_id LIKE ? OR c.source_group LIKE ? OR c.source_name LIKE ?)")
        q = f"%{query}%"; params += [q,q,q,q]
    if favorites_only: conds.append("c.favorite=1")
    if enabled_only: conds.append("c.enabled=1")
    if no_epg: conds.append("(c.tvg_id IS NULL OR c.tvg_id='' OR c.tvg_id LIKE 'dummy.%')")
    if group_id: conds.append("c.group_id=?"); params.append(group_id)
    sf, sp = _sid_filter(source_id, col="c.source_id")
    if sf:
        conds.append(sf.removeprefix(" AND ")); params += sp
    where = ("WHERE "+" AND ".join(conds)) if conds else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM channels c LEFT JOIN groups_ g ON c.group_id=g.id {where}", params).fetchone()[0]
    rows = conn.execute(
        f"""SELECT c.*, g.name AS group_name FROM channels c
            LEFT JOIN groups_ g ON c.group_id=g.id {where}
            ORDER BY c.name COLLATE NOCASE LIMIT ? OFFSET ?""", params+[limit, offset]).fetchall()
    return _rl(rows), total

def get_favorites(limit=200, source_id=None):
    results, _ = search_channels(favorites_only=True, limit=limit, source_id=source_id)
    return results

def channel_stats(source_id=None):
    conn = _conn()
    sf, sp = _sid_filter(source_id)
    row = conn.execute(
        f"""SELECT COUNT(*) AS total,
                   SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled,
                   SUM(CASE WHEN favorite=1 THEN 1 ELSE 0 END) AS fav
            FROM channels WHERE 1=1{sf}""", sp).fetchone()
    g = conn.execute("SELECT COUNT(*) FROM groups_").fetchone()[0]
    return {"total_channels": row["total"] or 0, "enabled_channels": row["enabled"] or 0,
            "favorite_channels": row["fav"] or 0, "total_groups": g}

def get_all_channels_raw(source_id=None):
    conn = _conn()
    sf, sp = _sid_filter(source_id)
    rows = conn.execute(
        f"""SELECT c.* FROM channels c
            WHERE 1=1{sf} ORDER BY c.sort_order ASC""", sp).fetchall()
    return _rl(rows)

def update_channel_group_bulk(assignments):
    if not assignments: return
    conn = _conn()
    conn.executemany("UPDATE channels SET group_id=?, sort_order=? WHERE id=?",
                     [(gid,order,cid) for cid,gid,order in assignments])
    conn.commit()

def set_all_channels_group(gid):
    conn = _conn()
    conn.execute("UPDATE channels SET group_id=?", (gid,))
    conn.commit()


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

def list_groups(source_id=None):
    conn = _conn()
    sf, sp = _sid_filter(source_id, col="c.source_id")
    rows = conn.execute(
        f"""SELECT g.*, COUNT(c.id) AS channel_count,
                   SUM(CASE WHEN c.enabled=1 THEN 1 ELSE 0 END) AS enabled_count
            FROM groups_ g LEFT JOIN channels c ON c.group_id=g.id{' AND 1=1' + sf if sf else ''}
            GROUP BY g.id ORDER BY g.pinned DESC, g.sort_order ASC""", sp).fetchall()
    # Batch-load all rules in one query instead of N+1 per-group queries
    all_rules = get_all_rules_by_group()
    out = []
    for r in rows:
        d = dict(r); d["match_rules"] = all_rules.get(d["id"], [])
        out.append(d)
    # Categories are filters/labels only. The editor must show the same order
    # exported to players, so category assignment never forms a second order.
    out.sort(key=lambda g: (0 if g.get("pinned") else 1, g.get("sort_order", 0)))
    return out

def get_group(gid):
    conn = _conn()
    row = conn.execute("SELECT * FROM groups_ WHERE id=?", (gid,)).fetchone()
    if not row: return None
    d = dict(row); d["match_rules"] = get_group_rules(gid); return d

def create_group(name, is_custom=True, is_unmatched=False, parent_id=None):
    conn = _conn()
    gid = new_id()
    mx = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM groups_").fetchone()[0]
    conn.execute("INSERT INTO groups_ (id,name,enabled,pinned,is_custom,is_unmatched,sort_order,parent_id) VALUES (?,?,1,0,?,?,?,?)",
                 (gid, name, 1 if is_custom else 0, 1 if is_unmatched else 0, mx, parent_id))
    conn.commit()
    return get_group(gid)

def update_group(gid, **kw):
    conn = _conn()
    sets, params = [], []
    for k, v in kw.items():
        if k in {"name","enabled","pinned","sort_order","teamarr","name_epg","category",
                 "export_tag","parent_id"} and v is not None:
            sets.append(f"{k}=?"); params.append(v)
    if not sets: return get_group(gid)
    params.append(gid)
    conn.execute(f"UPDATE groups_ SET {','.join(sets)} WHERE id=?", params)
    conn.commit()
    return get_group(gid)

def delete_group(gid):
    conn = _conn()
    um = conn.execute("SELECT id FROM groups_ WHERE is_unmatched=1 LIMIT 1").fetchone()
    target = um["id"] if um else None
    conn.execute("UPDATE channels SET group_id=? WHERE group_id=?", (target, gid))
    conn.execute("DELETE FROM rules WHERE group_id=?", (gid,))
    conn.execute("DELETE FROM groups_ WHERE id=?", (gid,))
    conn.commit()

def reorder_groups(gids):
    conn = _conn()
    for i, gid in enumerate(gids):
        conn.execute("UPDATE groups_ SET sort_order=? WHERE id=?", (i, gid))
    conn.commit()

def ensure_unmatched_group():
    conn = _conn()
    row = conn.execute("SELECT * FROM groups_ WHERE is_unmatched=1 LIMIT 1").fetchone()
    if row:
        d = dict(row); d["match_rules"] = []; return d
    return create_group("Unmatched", is_custom=False, is_unmatched=True)

def clear_groups():
    conn = _conn()
    conn.execute("DELETE FROM rules"); conn.execute("DELETE FROM groups_")
    conn.commit()

def find_group_by_name(name):
    conn = _conn()
    row = conn.execute("SELECT * FROM groups_ WHERE LOWER(name)=LOWER(?) LIMIT 1", (name,)).fetchone()
    return _rd(row)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

def get_group_rules(gid):
    conn = _conn()
    rows = conn.execute("SELECT * FROM rules WHERE group_id=? ORDER BY sort_order", (gid,)).fetchall()
    return _rl(rows)

def add_rule(gid, field, pattern, match_type="contains"):
    conn = _conn()
    rid = new_id()
    mx = conn.execute("SELECT COALESCE(MAX(sort_order),-1)+1 FROM rules WHERE group_id=?", (gid,)).fetchone()[0]
    conn.execute("INSERT INTO rules (id,group_id,field,pattern,match_type,sort_order) VALUES (?,?,?,?,?,?)",
                 (rid, gid, field, pattern, match_type, mx))
    conn.commit()
    return {"id":rid,"group_id":gid,"field":field,"pattern":pattern,"match_type":match_type,"sort_order":mx}

def delete_rule(rid):
    conn = _conn()
    conn.execute("DELETE FROM rules WHERE id=?", (rid,))
    conn.commit()

def update_rule(rid, **kw):
    conn = _conn()
    sets, params = [], []
    ok = {"field", "pattern", "match_type", "sort_order"}
    for k, v in kw.items():
        if k in ok and v is not None:
            sets.append(f"{k}=?"); params.append(v)
    if not sets: return
    params.append(rid)
    conn.execute(f"UPDATE rules SET {','.join(sets)} WHERE id=?", params)
    conn.commit()

def get_rule(rid):
    conn = _conn()
    row = conn.execute("SELECT * FROM rules WHERE id=?", (rid,)).fetchone()
    return _rd(row)

def find_duplicate_channels(source_id=None):
    """Find channels sharing the same URL."""
    conn = _conn()
    sf, sp = _sid_filter(source_id, col="c.source_id")
    rows = conn.execute(
        f"""SELECT c.url, COUNT(*) as cnt,
                   GROUP_CONCAT(c.id, '||') as channel_ids,
                   GROUP_CONCAT(c.name, '||') as channel_names,
                   GROUP_CONCAT(COALESCE(g.name,''), '||') as group_names
            FROM channels c
            LEFT JOIN groups_ g ON c.group_id=g.id
            WHERE c.url != ''{sf}
            GROUP BY c.url HAVING cnt > 1
            ORDER BY cnt DESC""", sp).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        ids = d["channel_ids"].split("||")
        names = d["channel_names"].split("||")
        groups = d["group_names"].split("||")
        d["channels"] = [{"id": ids[i], "name": names[i], "group": groups[i]} for i in range(len(ids))]
        del d["channel_ids"], d["channel_names"], d["group_names"]
        result.append(d)
    return result

def bulk_rename_channels(pattern, replacement, is_regex=False, source_id=None):
    """Find/replace in channel names. Returns count of modified channels."""
    conn = _conn()
    sf, sp = _sid_filter(source_id)
    rows = conn.execute(f"SELECT id, name FROM channels WHERE 1=1{sf}", sp).fetchall()
    modified = 0
    for r in rows:
        old_name = r["name"]
        if is_regex:
            new_name = re.sub(pattern, replacement, old_name, flags=re.IGNORECASE)
        else:
            # Case-insensitive literal replace
            idx = old_name.lower().find(pattern.lower())
            if idx == -1:
                continue
            new_name = old_name[:idx] + replacement + old_name[idx+len(pattern):]
        if new_name != old_name:
            conn.execute("UPDATE channels SET name=? WHERE id=?", (new_name, r["id"]))
            modified += 1
    conn.commit()
    return modified

def get_all_rules_by_group():
    conn = _conn()
    rows = conn.execute("SELECT * FROM rules ORDER BY sort_order").fetchall()
    out = {}
    for r in rows:
        d = dict(r); out.setdefault(d["group_id"], []).append(d)
    return out


# ---------------------------------------------------------------------------
# EPG Sources
# ---------------------------------------------------------------------------

def list_epg_sources():
    conn = _conn()
    rows = conn.execute("SELECT * FROM epg_sources ORDER BY created_at DESC").fetchall()
    return _rl(rows)

def add_epg_source(name, url):
    conn = _conn()
    cur = conn.execute("INSERT INTO epg_sources (name,url) VALUES (?,?)", (name, url))
    eid = cur.lastrowid; conn.commit()
    row = conn.execute("SELECT * FROM epg_sources WHERE id=?", (eid,)).fetchone()
    return dict(row)

def delete_epg_source(eid):
    conn = _conn()
    conn.execute("DELETE FROM epg_sources WHERE id=?", (eid,))
    conn.commit()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key, default=""):
    conn = _conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default

def set_setting(key, value):
    conn = _conn()
    conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit()

def get_all_settings():
    conn = _conn()
    rows = conn.execute("SELECT * FROM settings").fetchall()
    return {r["key"]:r["value"] for r in rows}


# ---------------------------------------------------------------------------
# Export History
# ---------------------------------------------------------------------------

def add_export_history(channel_count, group_count, m3u_size, xml_size):
    conn = _conn()
    conn.execute(
        "INSERT INTO export_history (channel_count,group_count,m3u_size,xml_size) VALUES (?,?,?,?)",
        (channel_count, group_count, m3u_size, xml_size))
    # Keep only last 100 entries
    conn.execute("""DELETE FROM export_history WHERE id NOT IN
                    (SELECT id FROM export_history ORDER BY id DESC LIMIT 100)""")
    conn.commit()

def get_export_history(limit=50):
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM export_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return _rl(rows)


# ---------------------------------------------------------------------------
# Group Templates
# ---------------------------------------------------------------------------

def list_group_templates():
    conn = _conn()
    rows = conn.execute("SELECT * FROM group_templates ORDER BY created_at DESC").fetchall()
    import json as _j
    result = []
    for r in rows:
        d = dict(r)
        try: d["rules"] = _j.loads(d.get("rules_json") or "[]")
        except Exception: d["rules"] = []
        result.append(d)
    return result

def get_group_template(tid):
    conn = _conn()
    row = conn.execute("SELECT * FROM group_templates WHERE id=?", (tid,)).fetchone()
    if not row: return None
    import json as _j
    d = dict(row)
    try: d["rules"] = _j.loads(d.get("rules_json") or "[]")
    except Exception: d["rules"] = []
    return d

def save_group_template(name, category="", enabled=1, pinned=0, teamarr=0,
                        name_epg=0, export_tag="", rules=None):
    import json as _j
    conn = _conn()
    tid = new_id()
    rules_json = _j.dumps(rules or [])
    conn.execute(
        """INSERT INTO group_templates
           (id,name,category,enabled,pinned,teamarr,name_epg,export_tag,rules_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (tid, name, category, 1 if enabled else 0, 1 if pinned else 0,
         1 if teamarr else 0, 1 if name_epg else 0, export_tag, rules_json))
    conn.commit()
    return get_group_template(tid)

def delete_group_template(tid):
    conn = _conn()
    conn.execute("DELETE FROM group_templates WHERE id=?", (tid,))
    conn.commit()


# ---------------------------------------------------------------------------
# Channel Health
# ---------------------------------------------------------------------------

def set_channel_health(channel_id, status_code=None, latency_ms=None, error=None):
    conn = _conn()
    conn.execute(
        """INSERT OR REPLACE INTO channel_health
           (channel_id,status_code,latency_ms,error,checked_at)
           VALUES (?,?,?,?,datetime('now'))""",
        (channel_id, status_code, latency_ms, error))
    conn.commit()

def get_channel_health(channel_ids=None):
    conn = _conn()
    if channel_ids:
        ph = ",".join("?" * len(channel_ids))
        rows = conn.execute(
            f"SELECT * FROM channel_health WHERE channel_id IN ({ph})", channel_ids).fetchall()
    else:
        rows = conn.execute("SELECT * FROM channel_health").fetchall()
    return {r["channel_id"]: dict(r) for r in rows}

def clear_channel_health():
    conn = _conn()
    conn.execute("DELETE FROM channel_health")
    conn.commit()


# ---------------------------------------------------------------------------
# EPG Coverage
# ---------------------------------------------------------------------------

def get_epg_coverage():
    """Return counts of channels broken down by EPG status."""
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) FROM channels WHERE smart_group_id IS NULL").fetchone()[0]
    matched = conn.execute(
        "SELECT COUNT(*) FROM channels WHERE smart_group_id IS NULL"
        " AND tvg_id != '' AND tvg_id NOT LIKE 'dummy.%' AND tvg_id NOT LIKE 'dummy-%'").fetchone()[0]
    dummy = conn.execute(
        "SELECT COUNT(*) FROM channels WHERE smart_group_id IS NULL"
        " AND (tvg_id LIKE 'dummy.%' OR tvg_id LIKE 'dummy-%')").fetchone()[0]
    unmatched = total - matched - dummy
    # Top 20 groups with lowest EPG coverage
    rows = conn.execute("""
        SELECT g.name, COUNT(c.id) AS total_ch,
               SUM(CASE WHEN c.tvg_id != '' AND c.tvg_id NOT LIKE 'dummy.%'
                             AND c.tvg_id NOT LIKE 'dummy-%' THEN 1 ELSE 0 END) AS matched_ch
        FROM groups_ g
        JOIN channels c ON c.group_id = g.id
        WHERE c.smart_group_id IS NULL
        GROUP BY g.id
        HAVING total_ch > 0
        ORDER BY (CAST(matched_ch AS REAL) / total_ch) ASC
        LIMIT 20
    """).fetchall()
    groups = [{"name": r["name"], "total": r["total_ch"], "matched": r["matched_ch"]} for r in rows]
    return {
        "total": total,
        "matched": matched,
        "dummy": dummy,
        "unmatched": unmatched,
        "coverage_pct": round(100 * matched / total, 1) if total else 0,
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------

def export_all_data():
    conn = _conn()
    data = {
        "version": 3,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "sources": _rl(conn.execute("SELECT * FROM sources").fetchall()),
        "groups": _rl(conn.execute("SELECT * FROM groups_").fetchall()),
        "channels": _rl(conn.execute("SELECT * FROM channels").fetchall()),
        "rules": _rl(conn.execute("SELECT * FROM rules").fetchall()),
        "epg_sources": _rl(conn.execute("SELECT * FROM epg_sources").fetchall()),
        "group_templates": _rl(conn.execute("SELECT * FROM group_templates").fetchall()),
        "chno_map": _rl(conn.execute("SELECT * FROM chno_map").fetchall()),
        "channel_tombstones": _rl(conn.execute("SELECT * FROM channel_tombstones").fetchall()),
        "teamarr_category_ids": _rl(conn.execute("SELECT * FROM teamarr_category_ids").fetchall()),
        "teamarr_stream_ids": _rl(conn.execute("SELECT * FROM teamarr_stream_ids").fetchall()),
        "settings": {r["key"]:r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()},
    }
    return data

def _table_columns(conn, table):
    """Live column names for a table, in declared order."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]

def _insert_rows(conn, table, rows):
    """Insert a list of dict rows into ``table`` using only the columns that
    exist in BOTH the backup row and the live schema. This makes restore
    resilient to schema drift in either direction — a backup taken before a
    column was added (extra live columns fall back to their defaults) and a
    backup taken after a column was removed (stale keys are ignored) both
    import cleanly instead of raising a positional-arity error."""
    if not rows:
        return
    live = set(_table_columns(conn, table))
    for row in rows:
        cols = [c for c in row.keys() if c in live]
        if not cols:
            continue
        placeholders = ",".join("?" for _ in cols)
        collist = ",".join(cols)
        conn.execute(
            f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
            [row[c] for c in cols],
        )

def import_all_data(data):
    """Atomically replace all primary data with a restored backup.

    Everything runs inside a single transaction: if any step fails the whole
    restore rolls back and the existing data is left untouched, instead of the
    old behavior which DELETE'd every table first and could leave the DB half
    wiped if a later INSERT raised.
    """
    conn = _conn()
    try:
        conn.execute("BEGIN")
        for t in ("teamarr_stream_ids", "teamarr_category_ids", "chno_map",
                  "channel_tombstones", "channels", "rules", "groups_",
                  "group_templates", "epg_sources", "sources", "settings"):
            conn.execute(f"DELETE FROM {t}")
        _insert_rows(conn, "sources", data.get("sources", []))
        # Backup key is "groups"; table is "groups_".
        _insert_rows(conn, "groups_", data.get("groups", []))
        _insert_rows(conn, "channels", data.get("channels", []))
        _insert_rows(conn, "rules", data.get("rules", []))
        _insert_rows(conn, "epg_sources", data.get("epg_sources", []))
        _insert_rows(conn, "group_templates", data.get("group_templates", []))
        _insert_rows(conn, "chno_map", data.get("chno_map", []))
        _insert_rows(conn, "channel_tombstones", data.get("channel_tombstones", []))
        _insert_rows(conn, "teamarr_category_ids", data.get("teamarr_category_ids", []))
        _insert_rows(conn, "teamarr_stream_ids", data.get("teamarr_stream_ids", []))
        for k, v in data.get("settings", {}).items():
            conn.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (k, v))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Smart Groups (EPG-based auto-populated groups)
# ---------------------------------------------------------------------------

def delete_smart_group_channels(smart_group_id):
    """Remove all shadow channels belonging to a smart group."""
    conn = _conn()
    conn.execute("DELETE FROM channels WHERE smart_group_id=?", (smart_group_id,))
    conn.commit()

def delete_all_smart_group_channels():
    """Remove ALL smart group shadow channels."""
    conn = _conn()
    conn.execute("DELETE FROM channels WHERE smart_group_id IS NOT NULL")
    conn.commit()

def insert_smart_group_channels(channels, smart_group_id, group_id):
    """Insert shadow channel copies for a smart group."""
    if not channels:
        return
    conn = _conn()
    conn.executemany(
        """INSERT OR REPLACE INTO channels
           (id,source_id,stream_id,name,source_name,source_group,
            group_id,tvg_id,original_tvg_id,tvg_name,logo,url,enabled,favorite,
            sort_order,channel_number,smart_group_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(stable_shadow_id(smart_group_id, c), c["source_id"], c.get("stream_id"),
          c["name"], c.get("source_name",""), c.get("source_group",""),
          group_id, c.get("tvg_id",""), c.get("original_tvg_id",""),
          c.get("tvg_name",""), c.get("logo",""), c.get("url",""),
          1, 0, c.get("sort_order",0), c.get("channel_number",""),
          smart_group_id) for c in channels])
    conn.commit()
