"""Lineup snapshots, diffs, action history, and safe undo.

Snapshots intentionally exclude sources, integration settings, and credentials.
They capture only the curated lineup state that a rule, import, reorder, or bulk
operation can damage. Payloads are compressed because large providers can have
tens of thousands of channels.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
import zlib
from datetime import datetime, timezone
from typing import Any

from app.db import connection


SNAPSHOT_TABLES = (
    "groups_", "rules", "chno_map", "teamarr_category_ids", "teamarr_stream_ids",
)

CHANNEL_SNAPSHOT_COLUMNS = (
    "id", "source_id", "stream_id", "name", "source_name", "source_group",
    "group_id", "tvg_id", "original_tvg_id", "tvg_name", "logo", "enabled",
    "favorite", "sort_order", "channel_number", "smart_group_id", "source_order",
    "placement_locked", "name_locked",
)


def _rows(conn, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]


def _state(conn=None) -> dict[str, Any]:
    conn = conn or connection()
    live_channel_columns = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
    projection = [column for column in CHANNEL_SNAPSHOT_COLUMNS if column in live_channel_columns]
    return {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tables": {
            **{table: _rows(conn, table) for table in SNAPSHOT_TABLES},
            "channels": [dict(row) for row in conn.execute(
                f"SELECT {','.join(projection)} FROM channels"
            ).fetchall()],
        },
    }


def _encode(state: dict[str, Any]) -> tuple[bytes, str]:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return zlib.compress(raw, level=6), hashlib.sha256(raw).hexdigest()


def _decode(payload: bytes, checksum: str) -> dict[str, Any]:
    raw = zlib.decompress(payload)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), checksum):
        raise ValueError("Snapshot checksum does not match")
    state = json.loads(raw)
    if state.get("format") != 1 or not isinstance(state.get("tables"), dict):
        raise ValueError("Unsupported lineup snapshot format")
    return state


def create_snapshot(label: str, kind: str = "manual", *, dedupe_seconds: int = 0) -> dict[str, Any]:
    conn = connection()
    clean_label = (label or "Lineup snapshot").strip()[:120]
    clean_kind = (kind or "manual").strip()[:32]
    if dedupe_seconds:
        prior = conn.execute(
            """SELECT * FROM lineup_snapshots WHERE label=? AND kind=?
               AND created_at >= datetime('now', ?) ORDER BY created_at DESC LIMIT 1""",
            (clean_label, clean_kind, f"-{int(dedupe_seconds)} seconds"),
        ).fetchone()
        if prior:
            return _summary(dict(prior))
    state = _state(conn)
    payload, checksum = _encode(state)
    sid = str(uuid.uuid4())
    channels = len(state["tables"]["channels"])
    groups = len(state["tables"]["groups_"])
    conn.execute(
        """INSERT INTO lineup_snapshots
           (id,label,kind,payload,checksum,channel_count,group_count)
           VALUES (?,?,?,?,?,?,?)""",
        (sid, clean_label, clean_kind, payload, checksum, channels, groups),
    )
    # Automatic safety points are bounded; named/manual snapshots are retained.
    conn.execute(
        """DELETE FROM lineup_snapshots WHERE kind='automatic' AND id NOT IN
           (SELECT id FROM lineup_snapshots WHERE kind='automatic'
            ORDER BY created_at DESC, rowid DESC LIMIT 30)"""
    )
    conn.commit()
    return {
        "id": sid, "label": clean_label, "kind": clean_kind,
        "channel_count": channels, "group_count": groups,
        "created_at": state["created_at"],
    }


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in (
        "id", "label", "kind", "channel_count", "group_count", "created_at"
    )}


def list_snapshots(limit: int = 50) -> list[dict[str, Any]]:
    rows = connection().execute(
        """SELECT id,label,kind,channel_count,group_count,created_at
           FROM lineup_snapshots ORDER BY created_at DESC, rowid DESC LIMIT ?""",
        (max(1, min(int(limit), 200)),),
    ).fetchall()
    return [_summary(dict(row)) for row in rows]


def delete_snapshot(snapshot_id: str) -> bool:
    conn = connection()
    cur = conn.execute("DELETE FROM lineup_snapshots WHERE id=?", (snapshot_id,))
    conn.commit()
    return bool(cur.rowcount)


def _insert_rows(conn, table: str, rows: list[dict[str, Any]]) -> None:
    live = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for row in rows:
        cols = [key for key in row if key in live]
        if not cols:
            continue
        conn.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [row[key] for key in cols],
        )


def restore_snapshot(snapshot_id: str) -> dict[str, Any]:
    conn = connection()
    row = conn.execute("SELECT * FROM lineup_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise KeyError(snapshot_id)
    state = _decode(row["payload"], row["checksum"])
    tables = state["tables"]
    target_channels = {channel["id"]: channel for channel in tables.get("channels", [])}
    try:
        conn.execute("BEGIN")
        # Restore safe structural tables first. Sources/settings and the private
        # tombstone store are deliberately preserved.
        for table in ("teamarr_stream_ids", "teamarr_category_ids", "chno_map", "rules", "groups_"):
            conn.execute(f"DELETE FROM {table}")
        for table in ("groups_", "rules"):
            _insert_rows(conn, table, tables.get(table, []))

        current = {dict(channel)["id"]: dict(channel) for channel in conn.execute("SELECT * FROM channels")}
        # Channels introduced after the snapshot become tombstones so undoing
        # the undo remains possible without copying credential-bearing URLs
        # into the snapshot payload.
        for cid in set(current) - set(target_channels):
            channel = current[cid]
            conn.execute(
                """INSERT OR REPLACE INTO channel_tombstones
                   (channel_id,source_id,stream_id,url,original_tvg_id,source_name,
                    source_group,row_json,removed_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                (cid, channel.get("source_id"), channel.get("stream_id"), channel.get("url") or "",
                 channel.get("original_tvg_id") or "", channel.get("source_name") or "",
                 channel.get("source_group") or "", json.dumps(channel, separators=(",", ":"))),
            )
            conn.execute("DELETE FROM channels WHERE id=?", (cid,))

        live_columns = {row[1] for row in conn.execute("PRAGMA table_info(channels)")}
        for cid, desired in target_channels.items():
            if cid not in current:
                tomb = conn.execute(
                    "SELECT * FROM channel_tombstones WHERE channel_id=?", (cid,)
                ).fetchone()
                if tomb:
                    restored = json.loads(tomb["row_json"])
                    cols = [column for column in restored if column in live_columns]
                    conn.execute(
                        f"INSERT INTO channels ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                        [restored[column] for column in cols],
                    )
                    conn.execute("DELETE FROM channel_tombstones WHERE channel_id=?", (cid,))
                else:
                    # A credential-free snapshot cannot recreate a station
                    # whose private stream row no longer exists anywhere.
                    continue
            fields = [key for key in desired if key != "id" and key in live_columns]
            if fields:
                conn.execute(
                    f"UPDATE channels SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
                    [desired[key] for key in fields] + [cid],
                )

        for table in ("chno_map", "teamarr_category_ids", "teamarr_stream_ids"):
            _insert_rows(conn, table, tables.get(table, []))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _summary(dict(row))


def snapshot_diff(snapshot_id: str) -> dict[str, Any]:
    conn = connection()
    row = conn.execute("SELECT * FROM lineup_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise KeyError(snapshot_id)
    old = _decode(row["payload"], row["checksum"])["tables"]
    current = _state(conn)["tables"]
    old_channels = {r["id"]: r for r in old.get("channels", [])}
    cur_channels = {r["id"]: r for r in current.get("channels", [])}
    added = sorted(set(cur_channels) - set(old_channels))
    removed = sorted(set(old_channels) - set(cur_channels))
    moved = []
    changed = []
    for cid in set(old_channels) & set(cur_channels):
        before, after = old_channels[cid], cur_channels[cid]
        if (before.get("group_id"), before.get("sort_order")) != (after.get("group_id"), after.get("sort_order")):
            moved.append(cid)
        if any(before.get(key) != after.get(key) for key in
               ("name", "tvg_id", "enabled", "placement_locked", "favorite")):
            changed.append(cid)
    old_groups = {r["id"]: r for r in old.get("groups_", [])}
    cur_groups = {r["id"]: r for r in current.get("groups_", [])}
    return {
        "snapshot": _summary(dict(row)),
        "channels": {"added": len(added), "removed": len(removed),
                     "moved": len(moved), "changed": len(changed)},
        "groups": {"added": len(set(cur_groups) - set(old_groups)),
                   "removed": len(set(old_groups) - set(cur_groups))},
        "sample": {
            "added": [cur_channels[c].get("name") for c in added[:10]],
            "removed": [old_channels[c].get("name") for c in removed[:10]],
            "moved": [cur_channels[c].get("name") for c in moved[:10]],
        },
    }


def record_action(action: str, summary: str = "", snapshot_id: str | None = None,
                  status: str = "completed") -> None:
    conn = connection()
    conn.execute(
        "INSERT INTO action_history(action,summary,snapshot_id,status) VALUES (?,?,?,?)",
        ((action or "change")[:80], (summary or "")[:500], snapshot_id, status[:24]),
    )
    conn.execute(
        """DELETE FROM action_history WHERE id NOT IN
           (SELECT id FROM action_history ORDER BY id DESC LIMIT 300)"""
    )
    conn.commit()


def list_actions(limit: int = 100) -> list[dict[str, Any]]:
    rows = connection().execute(
        """SELECT h.*, s.label AS snapshot_label FROM action_history h
           LEFT JOIN lineup_snapshots s ON s.id=h.snapshot_id
           ORDER BY h.id DESC LIMIT ?""",
        (max(1, min(int(limit), 300)),),
    ).fetchall()
    return [dict(row) for row in rows]


def undo_latest() -> dict[str, Any]:
    row = connection().execute(
        """SELECT h.* FROM action_history h JOIN lineup_snapshots s ON s.id=h.snapshot_id
           WHERE h.status='completed' AND h.action<>'undo'
           ORDER BY h.id DESC LIMIT 1"""
    ).fetchone()
    if not row:
        raise LookupError("No undo point is available")
    safety = create_snapshot("Before undo", "automatic")
    restored = restore_snapshot(row["snapshot_id"])
    record_action("undo", f"Restored state before {row['action']}", safety["id"])
    return {"restored": restored, "previous_action": dict(row), "redo_snapshot": safety}
