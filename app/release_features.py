"""Post-alpha product features exposed as a self-contained FastAPI router."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree as ET

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app import history
from app.db import connection, get_active_source_ids, get_all_channels_raw, get_all_settings, get_source, list_groups
from app.importers import import_channels_from_m3u, import_channels_from_xc


class SnapshotRequest(BaseModel):
    label: str = Field("Named snapshot", min_length=1, max_length=120)


class BackupValidationRequest(BaseModel):
    backup: dict[str, Any]


def _preview_sync(source_id: int, fresh: list[dict[str, Any]]) -> dict[str, Any]:
    existing = get_all_channels_raw(source_id=source_id)
    by_stream = {row.get("stream_id"): row for row in existing if row.get("stream_id") is not None}
    by_url = {row.get("url"): row for row in existing if row.get("url")}
    matched: set[str] = set()
    added, changed, unchanged = [], [], 0
    for channel in fresh:
        old = by_stream.get(channel.get("stream_id")) if channel.get("stream_id") is not None else None
        if old is None and channel.get("url"):
            old = by_url.get(channel.get("url"))
        if old is None or old.get("id") in matched:
            added.append(channel)
            continue
        matched.add(old["id"])
        fields = [key for key in ("source_name", "source_group", "tvg_id", "logo", "channel_number")
                  if (old.get("original_tvg_id") if key == "tvg_id" else old.get(key)) != channel.get(key)]
        if fields:
            changed.append({"id": old["id"], "name": old.get("name"), "fields": fields,
                            "from_group": old.get("source_group"), "to_group": channel.get("source_group")})
        else:
            unchanged += 1
    removed = [row for row in existing if row.get("id") not in matched]
    locked_removed = [row for row in removed if row.get("placement_locked")]
    return {
        "source_id": source_id, "fresh_count": len(fresh), "current_count": len(existing),
        "added": len(added), "changed": len(changed), "removed": len(removed),
        "unchanged": unchanged, "locked_removals": len(locked_removed),
        "requires_review": bool(removed or locked_removed),
        "sample": {
            "added": [{"name": r.get("name"), "group": r.get("source_group")} for r in added[:25]],
            "changed": changed[:25],
            "removed": [{"name": r.get("name"), "group": r.get("source_group"),
                         "locked": bool(r.get("placement_locked"))} for r in removed[:25]],
        },
    }


def _validate_backup(data: dict[str, Any]) -> dict[str, Any]:
    errors, warnings = [], []
    if not isinstance(data, dict):
        return {"valid": False, "errors": ["Backup must be a JSON object"], "warnings": []}
    version = data.get("version")
    if not isinstance(version, int):
        errors.append("Missing numeric backup version")
    elif version > 3:
        warnings.append(f"Backup version {version} is newer than this application")
    for key in ("sources", "groups", "channels", "rules"):
        if not isinstance(data.get(key), list):
            errors.append(f"{key} must be a list")
    sources = {row.get("id") for row in data.get("sources", []) if isinstance(row, dict)}
    groups = {row.get("id") for row in data.get("groups", []) if isinstance(row, dict)}
    channels = data.get("channels", []) if isinstance(data.get("channels"), list) else []
    orphan_sources = sum(1 for row in channels if isinstance(row, dict) and row.get("source_id") not in sources)
    orphan_groups = sum(1 for row in channels if isinstance(row, dict) and row.get("group_id") and row.get("group_id") not in groups)
    if orphan_sources:
        errors.append(f"{orphan_sources} channels reference missing sources")
    if orphan_groups:
        warnings.append(f"{orphan_groups} channels reference missing groups and will become ungrouped")
    ids = [row.get("id") for row in channels if isinstance(row, dict)]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate channel IDs found")
    return {
        "valid": not errors, "version": version, "errors": errors, "warnings": warnings,
        "counts": {"sources": len(sources), "groups": len(groups), "channels": len(channels),
                   "rules": len(data.get("rules", [])) if isinstance(data.get("rules"), list) else 0},
    }


def _epg_quality(export_dir: Path) -> dict[str, Any]:
    xml_path = export_dir / "organized.xml"
    if not xml_path.exists():
        return {"status": "missing", "summary": "Generate an export before running guide diagnostics."}
    names, programmes, generic, blank, latest_stop = {}, {}, {}, {}, None
    generic_re = re.compile(r"^(movie|movies|episode|program|programming|show|tv|24/7|to be announced|tba)$", re.I)
    try:
        for _, elem in ET.iterparse(str(xml_path), events=("end",)):
            tag = elem.tag.split("}", 1)[-1]
            if tag == "channel":
                cid = elem.attrib.get("id", "")
                display = elem.find("display-name")
                names[cid] = (display.text or cid) if display is not None else cid
                elem.clear()
            elif tag == "programme":
                cid = elem.attrib.get("channel", "")
                programmes[cid] = programmes.get(cid, 0) + 1
                title = elem.find("title")
                text = (title.text or "").strip() if title is not None else ""
                if not text:
                    blank[cid] = blank.get(cid, 0) + 1
                elif generic_re.match(text):
                    generic[cid] = generic.get(cid, 0) + 1
                stop = (elem.attrib.get("stop") or "")[:14]
                if stop and (latest_stop is None or stop > latest_stop):
                    latest_stop = stop
                elem.clear()
    except ET.ParseError as exc:
        return {"status": "invalid", "summary": str(exc)}
    empty = [cid for cid in names if not programmes.get(cid)]
    generic_ids = sorted(generic, key=generic.get, reverse=True)
    blank_ids = sorted(blank, key=blank.get, reverse=True)
    return {
        "status": "ok", "channels": len(names), "programmes": sum(programmes.values()),
        "empty_channels": len(empty), "generic_title_channels": len(generic_ids),
        "blank_title_channels": len(blank_ids), "latest_stop": latest_stop,
        "sample": {
            "empty": [{"id": cid, "name": names.get(cid, cid)} for cid in empty[:30]],
            "generic": [{"id": cid, "name": names.get(cid, cid), "rows": generic[cid]} for cid in generic_ids[:30]],
            "blank": [{"id": cid, "name": names.get(cid, cid), "rows": blank[cid]} for cid in blank_ids[:30]],
        },
    }


def create_router(schedule_export: Callable[[], None], export_dir: Path) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/safety/overview")
    def safety_overview():
        conn = connection()
        settings = get_all_settings()
        sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        groups = conn.execute("SELECT COUNT(*) FROM groups_").fetchone()[0]
        channels = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        locked = conn.execute("SELECT COUNT(*) FROM channels WHERE placement_locked=1").fetchone()[0]
        epg_sources = conn.execute("SELECT COUNT(*) FROM epg_sources").fetchone()[0]
        schema = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        steps = [
            {"id": "source", "label": "Add and activate a channel source", "done": sources > 0},
            {"id": "groups", "label": "Review imported groups and their order", "done": groups > 0 and channels > 0},
            {"id": "locks", "label": "Lock stations whose placement must never drift", "done": locked > 0},
            {"id": "epg", "label": "Configure and verify guide data", "done": epg_sources > 0 or bool(list(export_dir.glob("organized.xml")))},
            {"id": "feed", "label": "Protect playlist and XMLTV feeds with a token", "done": bool(os.environ.get("M3U_BOSS_FEED_TOKEN"))},
            {"id": "snapshot", "label": "Create the first named lineup snapshot", "done": bool(history.list_snapshots(1))},
        ]
        return {"steps": steps, "complete": sum(1 for step in steps if step["done"]),
                "total": len(steps), "schema_version": schema[0] if schema else "unknown",
                "stats": {"sources": sources, "groups": groups, "channels": channels, "locked": locked}}

    @router.get("/history")
    def get_history(limit: int = Query(100, ge=1, le=300)):
        return {"snapshots": history.list_snapshots(limit), "actions": history.list_actions(limit)}

    @router.post("/history/snapshots")
    def post_snapshot(body: SnapshotRequest):
        snap = history.create_snapshot(body.label, "manual")
        history.record_action("snapshot", f"Created {body.label}", snap["id"])
        return snap

    @router.get("/history/snapshots/{snapshot_id}/diff")
    def get_snapshot_diff(snapshot_id: str):
        try:
            return history.snapshot_diff(snapshot_id)
        except KeyError as exc:
            raise HTTPException(404, "Snapshot not found") from exc

    @router.post("/history/snapshots/{snapshot_id}/restore")
    def post_snapshot_restore(snapshot_id: str):
        safety = history.create_snapshot("Before snapshot restore", "automatic")
        try:
            restored = history.restore_snapshot(snapshot_id)
        except KeyError as exc:
            raise HTTPException(404, "Snapshot not found") from exc
        history.record_action("restore", f"Restored {restored['label']}", safety["id"])
        schedule_export()
        return {"ok": True, "restored": restored, "undo_snapshot": safety}

    @router.delete("/history/snapshots/{snapshot_id}")
    def remove_snapshot(snapshot_id: str):
        if not history.delete_snapshot(snapshot_id):
            raise HTTPException(404, "Snapshot not found")
        return {"ok": True}

    @router.post("/history/undo")
    def post_undo():
        try:
            result = history.undo_latest()
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        schedule_export()
        return {"ok": True, **result}

    @router.get("/sources/{source_id}/preview-refresh")
    def preview_refresh(source_id: int):
        source = get_source(source_id)
        if not source:
            raise HTTPException(404, "Source not found")
        try:
            if source["type"] == "xc":
                fresh, _ = import_channels_from_xc(source["xc_server"], source["xc_username"],
                                                    source["xc_password"], source["xc_output"], source_id)
            elif source["type"] == "m3u":
                fresh = import_channels_from_m3u(source["m3u_url"], source_id)
            else:
                raise HTTPException(400, "Unsupported source type")
        except Exception as exc:
            # Provider exceptions often embed credential-bearing URLs. Keep
            # those out of API responses and browser screenshots.
            raise HTTPException(
                400, "Preview fetch failed; verify the source configuration"
            ) from exc
        return _preview_sync(source_id, fresh)

    @router.post("/backup/validate")
    def validate_backup(body: BackupValidationRequest):
        return _validate_backup(body.backup)

    @router.get("/epg/quality")
    def epg_quality():
        return _epg_quality(export_dir)

    @router.get("/teamarr/compatibility")
    def teamarr_compatibility():
        settings = get_all_settings()
        enabled_groups = [g for g in list_groups(source_id=get_active_source_ids()) if g.get("teamarr") and g.get("enabled")]
        checks = [
            {"name": "Teamarr enabled", "ok": settings.get("teamarr_enabled") == "1"},
            {"name": "Username configured", "ok": bool(settings.get("teamarr_username"))},
            {"name": "Password configured", "ok": bool(settings.get("teamarr_password"))},
            {"name": "At least one exported category", "ok": bool(enabled_groups)},
            {"name": "Output is TS or HLS", "ok": settings.get("teamarr_output", "ts") in {"ts", "m3u8"}},
        ]
        return {
            "status": "ready" if all(c["ok"] for c in checks) else "needs-setup",
            "checks": checks, "categories": len(enabled_groups),
            "contract": {
                "live_only": True,
                "actions": ["get_live_categories", "get_live_streams", "get_short_epg", "get_simple_data_table"],
                "endpoints": ["/player_api.php", "/live/{user}/{pass}/{stream}", "/xmltv.php", "/teamarr.xml"],
                "unsupported": ["VOD catalog", "series catalog", "catch-up archive"],
            },
        }

    @router.get("/system/migrations")
    def migration_status():
        conn = connection()
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        required = {"lineup_snapshots", "action_history", "teamarr_category_ids", "teamarr_stream_ids"}
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {"schema_version": row[0] if row else None, "current": "3",
                "healthy": required <= present, "missing_tables": sorted(required - present)}

    return router
