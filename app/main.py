import copy
import base64
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache, wraps
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from app.models import (
    AutoMatchApply, BulkDelete, BulkFavorite, BulkLock, BulkLogo, BulkMove,
    BulkNameEpg, BulkRename, BulkToggle, ChannelMove, ChannelPatch, EPGPatch,
    EPGSourceAdd, EpgRepairPackRequest, GroupCreate, GroupPatch, GroupReorder,
    GroupTemplateSave, HealthCheckRequest, ImportM3U, ImportXC, LogoOverride,
    ReorderChannels, RuleAdd, RuleSandboxRequest, SettingsUpdate,
    SmartGroupsSettings,
)

from app.db import (
    activate_source, add_epg_source, add_m3u, add_rule, add_xc, assign_export_chnos,
    renumber_group_block, renumber_all_in_order,
    bulk_favorite_channels, bulk_move_channels, bulk_copy_channels, bulk_rename_channels,
    bulk_toggle_channels, bulk_lock_channels,
    channel_stats, clear_dummy_tvg_ids, clear_groups, clear_source_channels, create_group,
    delete_channel, delete_channels_bulk,
    delete_epg_source, delete_group, delete_rule, delete_source,
    delete_smart_group_channels, delete_all_smart_group_channels,
    ensure_unmatched_group, export_all_data, find_duplicate_channels,
    find_group_by_name,
    force_activate_source,
    get_active_source, get_active_source_id, get_active_source_ids,
    get_active_sources, get_all_channels_raw,
    get_all_rules_by_group, get_all_settings, get_channel, get_favorites,
    get_group, get_group_channels, get_group_max_sort_order, get_group_rules,
    get_rule, get_setting, get_source, import_all_data, init_db,
    insert_channels, insert_smart_group_channels,
    list_epg_sources, list_groups, list_sources, move_channel, new_id,
    reorder_group_channels, reorder_groups, search_channels, set_setting,
    sync_channels, update_channel, update_channel_group_bulk,
    update_group, update_rule, update_source,
    apply_source_order_to_groups, sort_sxm_group, maintain_group_archive,
    # New feature functions
    add_export_history, get_export_history,
    list_group_templates, get_group_template, save_group_template, delete_group_template,
    set_channel_health, get_channel_health, clear_channel_health,
    get_epg_coverage,
    get_teamarr_category_id, get_teamarr_stream_id,
    get_channel_by_teamarr_stream_id,
)
from app.epg import (
    parse_epg, enrich_tvg_ids, build_filtered_epg,
    _extract_callsign, _extract_epg_callsign, _extract_display_callsign,
    _epg_id_is_foreign, _canonical_channel_name, _channel_identity_key,
    _epg_name_matches, _channel_is_loop_like, _LOOP_CHANNEL_RE,
    _ota_diginet_alias,
)

BASE_DIR = Path(__file__).resolve().parent.parent
EXPORT_DIR = BASE_DIR / "exports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_TRACEBACKS = os.environ.get("M3U_BOSS_DEBUG_TRACEBACKS", "0") == "1"
DEBUG_ENDPOINTS = os.environ.get("M3U_BOSS_DEBUG_ENDPOINTS", "0") == "1"

_SECRET_SETTING_KEYS = {"teamarr_password", "dispatcharr_password", "ntfy_token"}

# Only settings that are rendered by the browser belong in /api/settings.
# Large internal indexes (notably epg_chno_baseline) used to make this tiny
# form endpoint a ~700 KB response and were downloaded twice on every visit.
_PUBLIC_SETTING_KEYS = {
    "teamarr_enabled", "teamarr_username", "teamarr_password", "teamarr_output",
    "teamarr_base_url", "refresh_interval_minutes", "epg_window_days",
    "dispatcharr_auto_refresh", "dispatcharr_url", "dispatcharr_username",
    "dispatcharr_password", "dispatcharr_m3u_account_id", "dispatcharr_epg_source_id",
    "ntfy_enabled", "ntfy_url", "ntfy_topic", "ntfy_token",
}


def _public_settings(settings: dict | None = None) -> dict:
    """Return UI settings without ever serializing stored credentials."""
    stored = settings if settings is not None else get_all_settings()
    safe = {key: stored[key] for key in _PUBLIC_SETTING_KEYS if key in stored}
    for key in _SECRET_SETTING_KEYS:
        value = safe.get(key)
        safe[f"{key}_configured"] = bool(value)
        safe[key] = ""
    return safe


def _public_source(source: dict) -> dict:
    """Return source metadata while keeping provider credentials server-side."""
    safe = dict(source)
    for key in ("xc_password", "m3u_url", "epg_url"):
        value = safe.get(key)
        safe[f"{key}_configured"] = bool(value)
        safe[key] = ""
    return safe

# Optional export-time stream rewrites. This is deliberately empty by default;
# deployments can provide a JSON object such as
# {"http://tuner:5004":"https://tuner.example.com"} without editing code.
try:
    _configured_rewrites = json.loads(os.environ.get("M3U_BOSS_STREAM_REWRITES", "{}"))
    _LOCAL_STREAM_HOST_REWRITES = tuple(_configured_rewrites.items()) \
        if isinstance(_configured_rewrites, dict) else ()
except (TypeError, ValueError):
    _LOCAL_STREAM_HOST_REWRITES = ()


def _rewrite_local_stream_url(url: str) -> str:
    for old_prefix, new_prefix in _LOCAL_STREAM_HOST_REWRITES:
        if url.startswith(old_prefix):
            rewritten = new_prefix + url[len(old_prefix):]
            return rewritten
    return url

# ── Debounced background re-export ────────────────────────────────
# Any editor mutation calls schedule_export() which waits 2s for rapid
# edits to settle, then re-generates the M3U + EPG files in background.
_export_timer: threading.Timer | None = None
_export_lock = threading.Lock()
_export_write_lock = threading.Lock()
_export_status = {"status": "idle", "time": None, "channels": 0, "error": None}
_epg_watchdog_status = {
    "last_check": None,
    "last_ok": None,
    "status": "unknown",
    "issues": [],
    "self_heal_attempted": False,
    "self_heal_success": None,
}
_last_epg_watchdog_run = 0.0
_EPG_WATCHDOG_INTERVAL_SEC = 15 * 60
# Guide is considered STALE if the newest programme in the EPG reaches less than
# this many hours past "now" — i.e. an upstream source froze and the guide is no
# longer advancing.
_EPG_STALE_HOURS = 2.0
_backend_errors_lock = threading.Lock()
_backend_errors: deque[dict[str, str]] = deque(maxlen=200)

# Per-EPG-source health during EXPORT: last success / last failure per URL, so
# a single bad guide that's being skipped mid-export is still visible in
# diagnostics instead of vanishing into a silent `continue`. (Distinct from
# `_epg_source_status`, which tracks the EPG *browse* fetch.)
_epg_export_status_lock = threading.Lock()
_epg_export_status: dict[str, dict[str, Any]] = {}

# Snapshot of the last export's EPG match coverage: how many exported channels
# got real guide data vs. only a dummy/event filler, plus a sample of the
# unmatched ones. Powers the EPG diagnostics endpoint (#6).
_epg_match_stats_lock = threading.Lock()
_epg_match_stats: dict[str, Any] = {}

# Snapshot of the last export's match-logo coverage: how many distinct matchup
# titles got a composite badge, and a sample of those skipped with the reason
# (not a matchup vs. a team that couldn't be resolved). Powers the logo
# coverage report (#16).
_logo_coverage_lock = threading.Lock()
_logo_coverage: dict[str, Any] = {}
log = logging.getLogger("m3u_boss")
_xc_account_probe_times: dict[int, float] = {}
_XC_ACCOUNT_PROBE_TTL_SEC = 5 * 60
_response_cache_lock = threading.Lock()
_response_cache: dict[str, dict[str, Any]] = {}
_xc_refresh_lock = threading.Lock()
_xc_refresh_inflight = False


def _redact_text(value: Any) -> str:
    """Remove common IPTV credentials before text reaches logs/diagnostics."""
    text = str(value)
    text = re.sub(
        r"(?i)([?&](?:username|user|password|pass|token|key)=)[^&#\s]+",
        r"\1REDACTED", text,
    )
    text = re.sub(r"(?i)(/live/)[^/\s]+/[^/\s]+/", r"\1REDACTED/REDACTED/", text)
    text = re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1REDACTED@", text)
    return text


def _record_backend_error(context: str, exc: Exception):
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "context": context,
        "error": _redact_text(f"{type(exc).__name__}: {exc}"),
    }
    with _backend_errors_lock:
        _backend_errors.append(entry)
    log.error("%s: %s", context, entry["error"])


def _record_epg_source_error(url: str, exc: Exception):
    with _epg_export_status_lock:
        st = _epg_export_status.setdefault(url, {})
        st["url"] = url
        st["last_error"] = _redact_text(f"{type(exc).__name__}: {exc}")
        st["last_error_at"] = datetime.now(timezone.utc).isoformat()
        st["consec_errors"] = st.get("consec_errors", 0) + 1
        count = st["consec_errors"]
    # Alert on the 2nd consecutive failure so transient blips stay quiet.
    if count == 2:
        label = (url.split("/")[-1] or url)[:50]
        _notify("M3U Boss: EPG source failing",
                _redact_text(f"{label} has failed {count} times in a row: {type(exc).__name__}: {exc}"),
                priority="high", tags="warning,tv")


def _record_epg_source_ok(url: str, channels: int | None = None, programmes: int | None = None):
    with _epg_export_status_lock:
        st = _epg_export_status.setdefault(url, {})
        st["url"] = url
        st["last_ok_at"] = datetime.now(timezone.utc).isoformat()
        if channels is not None:
            st["channels"] = channels
        if programmes is not None:
            st["programmes"] = programmes
        # A fresh success clears the standing error flag and consecutive counter.
        st.pop("last_error", None)
        st.pop("last_error_at", None)
        st.pop("consec_errors", None)


def _capture_epg_match_stats(selected, export_tvg_map, covered_ids):
    """Record how many of the just-exported channels got real EPG coverage.

    ``selected`` is the list of (group, channel) tuples being exported;
    ``covered_ids`` is the set of export ids that have real <programme> rows
    (before dummy/event filler). Stores a capped sample of unmatched channels
    so the diagnostics endpoint can show *which* channels are missing a guide.
    """
    total = 0
    matched = 0
    unmatched_sample: list[dict] = []
    for grp, ch in selected:
        total += 1
        export_id = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
        if export_id and export_id in covered_ids:
            matched += 1
        elif len(unmatched_sample) < 200:
            unmatched_sample.append({
                "name": ch.get("name") or ch.get("source_name") or "",
                "tvg_id": export_id,
                "group": (grp or {}).get("name", "") if isinstance(grp, dict) else "",
            })
    with _epg_match_stats_lock:
        _epg_match_stats.clear()
        _epg_match_stats.update({
            "at": datetime.now(timezone.utc).isoformat(),
            "total": total,
            "matched": matched,
            "unmatched": total - matched,
            "coverage_pct": round(100.0 * matched / total, 1) if total else 0.0,
            "unmatched_sample": unmatched_sample,
        })


def _notify(title: str, message: str, priority: str | None = None, tags: str | None = None):
    """Send an ntfy push if configured. Opt-in, best-effort, never raises.

    Reads the ntfy config from the DB in the CALLING thread, then hands off to
    _notify_send (which touches no database). Do NOT call this from a thread
    that must stay DB-free — use _notify_send with pre-fetched config instead."""
    if get_setting("ntfy_enabled") != "1":
        return
    base = (get_setting("ntfy_url") or "").strip().rstrip("/")
    topic = (get_setting("ntfy_topic") or "").strip()
    token = (get_setting("ntfy_token") or "").strip()
    _notify_send(base, topic, token, title, message, priority, tags)


def _notify_send(base: str, topic: str, token: str, title: str, message: str,
                 priority: str | None = None, tags: str | None = None):
    """Fire an ntfy push from PRE-FETCHED config — touches no database, so it is
    safe to call from background threads that must not open a SQLite connection.

    Fires in a background thread so a slow/unreachable ntfy server can't stall
    the caller (watchdog, refresh, dead-stream sweep)."""
    if not (base and topic):
        return

    def _send():
        try:
            headers = {"Title": title}
            if priority:
                headers["Priority"] = priority
            if tags:
                headers["Tags"] = tags
            if token:
                headers["Authorization"] = f"Bearer {token}"
            requests.post(f"{base}/{topic}", data=message.encode("utf-8"),
                          headers=headers, timeout=10)
        except Exception as exc:
            _record_backend_error("ntfy.send", exc)

    threading.Thread(target=_send, daemon=True).start()


def _dispatcharr_refresh_blocking(cfg: dict):
    """Ask Dispatcharr to refresh its M3U account + force an EPG re-import so a
    fresh export (new match logos / channels / guide) propagates immediately
    instead of on Dispatcharr's own cadence. Opt-in: no-ops unless configured.

    Runs in a background daemon thread. It MUST NOT touch SQLite: opening a fresh
    thread-local connection here fails with "disk I/O error" on the shfs-FUSE
    DB under post-export contention (the bug that silently broke this push since
    it shipped). So ALL config — Dispatcharr creds + ntfy — is read by the caller
    (_maybe_refresh_dispatcharr, which has a working connection) and passed in via
    ``cfg``. The JWT is short-lived and nothing extra is persisted. Best-effort."""
    base = (cfg.get("base") or "").strip().rstrip("/")
    user = (cfg.get("user") or "").strip()
    pw = cfg.get("pw") or ""
    acct = (cfg.get("acct") or "").strip()
    epg_id = (cfg.get("epg_id") or "").strip()
    if not (base and user and pw and acct):
        return
    try:
        # JWT auth — /api/accounts/token/ (NOT /api/token/, which 403s on CSRF).
        tok = requests.post(f"{base}/api/accounts/token/",
                            json={"username": user, "password": pw}, timeout=15)
        tok.raise_for_status()
        access = (tok.json() or {}).get("access")
        if not access:
            raise RuntimeError("no access token in Dispatcharr auth response")
        auth_headers = {"Authorization": f"Bearer {access}"}
        before = requests.get(
            f"{base}/api/m3u/accounts/{acct}/", headers=auth_headers, timeout=15
        )
        before.raise_for_status()
        before_updated = (before.json() or {}).get("updated_at")

        refresh_complete = False
        for attempt in range(2):
            r = requests.post(
                f"{base}/api/m3u/refresh/{acct}/", headers=auth_headers, timeout=30
            )
            r.raise_for_status()
            log.info("dispatcharr: triggered M3U refresh for account %s", acct)

            # The refresh endpoint returns 202 before Celery has applied the
            # new playlist. Wait for the account to enter/finish its refresh or
            # for its successful-refresh timestamp to advance. Importing EPG
            # immediately used to race the channel refresh and attach guide
            # data to the previous lineup.
            deadline = time.monotonic() + 900
            saw_running = False
            retry_closed_connection = False
            while time.monotonic() < deadline:
                time.sleep(3)
                state_r = requests.get(
                    f"{base}/api/m3u/accounts/{acct}/", headers=auth_headers, timeout=15
                )
                state_r.raise_for_status()
                state = state_r.json() or {}
                status_value = (state.get("status") or "").lower()
                if status_value in {"fetching", "parsing"}:
                    saw_running = True
                    continue
                if status_value == "error":
                    message = state.get("last_message") or "unknown error"
                    # v0.27.x can hand one Celery task an expired pooled
                    # psycopg connection. A fresh task succeeds; retry exactly
                    # once rather than leaving Dispatcharr on the old lineup.
                    if attempt == 0 and "connection is closed" in message.lower():
                        log.warning("dispatcharr: closed pooled connection; retrying refresh once")
                        time.sleep(10)
                        retry_closed_connection = True
                        break
                    raise RuntimeError(f"Dispatcharr M3U refresh failed: {message}")
                updated = state.get("updated_at")
                if (saw_running and status_value in {"success", "idle"}) or (
                    updated and updated != before_updated
                ):
                    refresh_complete = True
                    break
            if refresh_complete:
                break
            if retry_closed_connection:
                continue
            raise TimeoutError("Dispatcharr M3U refresh did not finish within 15 minutes")
        if not refresh_complete:
            raise RuntimeError("Dispatcharr M3U refresh failed after one safe retry")

        # Verify the unqualified API order because several Dispatcharr views and
        # clients rely on its default rather than adding ?ordering= themselves.
        order_r = requests.get(
            f"{base}/api/channels/channels/", headers=auth_headers, timeout=30
        )
        order_r.raise_for_status()
        payload = order_r.json()
        channel_rows = payload.get("results", []) if isinstance(payload, dict) else payload
        visible_numbers = []
        for row in channel_rows or []:
            try:
                visible_numbers.append(float(row.get("channel_number")))
            except (TypeError, ValueError):
                continue
        if any(a > b for a, b in zip(visible_numbers, visible_numbers[1:])):
            raise RuntimeError("Dispatcharr default channel API is not sorted ascending")
        log.info("dispatcharr: M3U refresh completed and ascending order verified")
        # Also force an EPG re-import so the guide propagates on OUR export
        # cadence instead of waiting on Dispatcharr's independent hourly EPG
        # poll (closes the phase-misalignment gap between the two timers).
        # /api/epg/import/ FORCES an off-schedule refresh, but ONLY when the
        # target EPG source id is passed in the body ({"id": N}); a bodyless
        # POST is a no-op (verified 2026-06-22). epg_id is the Dispatcharr EPG
        # source id (set in Settings → Integrations). Skipped if unset.
        # Isolated + best-effort: never blocks the playlist refresh above.
        if epg_id:
            try:
                er = requests.post(f"{base}/api/epg/import/",
                                   headers=auth_headers,
                                   json={"id": int(epg_id)}, timeout=30)
                er.raise_for_status()
                log.info("dispatcharr: triggered EPG re-import for source %s", epg_id)
            except Exception as exc:
                _record_backend_error("dispatcharr.epg_refresh", exc)
    except Exception as exc:
        _record_backend_error("dispatcharr.refresh", exc)
        # ntfy config is pre-fetched (see docstring) — use the DB-free sender.
        _notify_send(cfg.get("ntfy_base", ""), cfg.get("ntfy_topic", ""),
                     cfg.get("ntfy_token", ""),
                     "M3U Boss: Dispatcharr refresh failed",
                     f"Could not trigger Dispatcharr M3U refresh: {exc}",
                     priority="high", tags="warning")


def _maybe_refresh_dispatcharr():
    """Fire the Dispatcharr refresh in a background thread so the export call
    doesn't block on Dispatcharr's (sometimes slow) refresh.

    Reads ALL config here, in the caller's thread (which has a working SQLite
    connection). The push thread must stay DB-free — see
    _dispatcharr_refresh_blocking's docstring for why."""
    if get_setting("dispatcharr_auto_refresh") != "1":
        return
    ntfy_on = get_setting("ntfy_enabled") == "1"
    cfg = {
        "base": (get_setting("dispatcharr_url") or "").strip().rstrip("/"),
        "user": (get_setting("dispatcharr_username") or "").strip(),
        "pw": get_setting("dispatcharr_password") or "",
        "acct": (get_setting("dispatcharr_m3u_account_id") or "").strip(),
        "epg_id": (get_setting("dispatcharr_epg_source_id") or "").strip(),
        "ntfy_base": (get_setting("ntfy_url") or "").strip().rstrip("/") if ntfy_on else "",
        "ntfy_topic": (get_setting("ntfy_topic") or "").strip() if ntfy_on else "",
        "ntfy_token": (get_setting("ntfy_token") or "").strip() if ntfy_on else "",
    }
    threading.Thread(target=_dispatcharr_refresh_blocking, args=(cfg,), daemon=True).start()


def _refresh_xc_account_status(sources: list[dict], force: bool = False):
    """Refresh XC account metadata (expiry, status, connections) for all XC sources.

    Uses a short TTL cache to avoid hammering providers on repeated page loads.
    """
    now = time.time()
    for src in sources:
        if (src.get("type") or "").lower() != "xc":
            continue
        sid = src.get("id")
        if not isinstance(sid, int):
            continue
        last = _xc_account_probe_times.get(sid, 0)
        if not force and (now - last) < _XC_ACCOUNT_PROBE_TTL_SEC:
            continue
        _xc_account_probe_times[sid] = now
        try:
            acct = fetch_xc_account_info(src.get("xc_server", ""), src.get("xc_username", ""), src.get("xc_password", ""))
            if acct:
                update_source(
                    sid,
                    expires_at=acct.get("expires_at"),
                    max_connections=acct.get("max_connections"),
                    active_connections=acct.get("active_connections"),
                    status=acct.get("status", ""),
                )
        except Exception as exc:
            _record_backend_error(f"sources.refresh_account.{sid}", exc)


def _invalidate_response_cache(prefix: str | None = None):
    with _response_cache_lock:
        if prefix is None:
            _response_cache.clear()
            return
        dead = [k for k in _response_cache.keys() if k.startswith(prefix)]
        for k in dead:
            _response_cache.pop(k, None)


def _cached_response(key: str, ttl_sec: float, build_fn):
    now = time.time()
    with _response_cache_lock:
        hit = _response_cache.get(key)
        if hit and (now - hit["ts"]) < ttl_sec:
            return copy.deepcopy(hit["value"])
    value = build_fn()
    with _response_cache_lock:
        _response_cache[key] = {"ts": time.time(), "value": copy.deepcopy(value)}
    return value


# Keys whose background refresh is currently running, so we don't spawn
# duplicate rebuild threads for the same expensive computation.
_swr_inflight: set[str] = set()
_swr_inflight_lock = threading.Lock()


def _cached_response_swr(key: str, ttl_sec: float, build_fn, fallback):
    """Stale-while-revalidate cache.

    Never blocks the caller on an expensive rebuild: returns the most recent
    value immediately (even if stale) and refreshes in the background. If
    nothing has ever been cached, returns `fallback` and kicks off the build.

    This keeps request-path endpoints (e.g. /api/groups) fast even when the
    underlying computation — like parsing the full EPG XML — takes many
    seconds. The old behaviour rebuilt synchronously on expiry, so every
    ~5 minutes one editor action would hang for ~18s.
    """
    now = time.time()
    with _response_cache_lock:
        hit = _response_cache.get(key)
    fresh = hit and (now - hit["ts"]) < ttl_sec
    if fresh:
        return copy.deepcopy(hit["value"])

    def _refresh():
        try:
            value = build_fn()
            with _response_cache_lock:
                _response_cache[key] = {"ts": time.time(), "value": copy.deepcopy(value)}
        except Exception as exc:
            _record_backend_error(f"swr.refresh.{key}", exc)
        finally:
            with _swr_inflight_lock:
                _swr_inflight.discard(key)

    with _swr_inflight_lock:
        if key not in _swr_inflight:
            _swr_inflight.add(key)
            threading.Thread(target=_refresh, name=f"swr-{key}", daemon=True).start()

    if hit:
        return copy.deepcopy(hit["value"])
    return copy.deepcopy(fallback)


def _prewarm_dashboard_cache():
    """Populate the expensive dashboard caches in the background."""
    try:
        _get_group_coverage_map()
        api_dashboard()
        api_export_history(20)
        api_epg_coverage()
        api_epg_dashboard()
        api_sources_reliability()
        api_system_errors(30)
    except Exception as exc:
        _record_backend_error("dashboard.prewarm", exc)


def _prewarm_dashboard_cache_async():
    t = threading.Thread(target=_prewarm_dashboard_cache, name="dashboard-prewarm", daemon=True)
    t.start()


def _get_group_coverage_map() -> dict[str, dict[str, int]]:
    def _build():
        idx = _build_export_programme_index()
        counts = idx["counts"]
        synthetic = idx["synthetic"]
        dummy = idx["dummy"]
        asid = get_active_source_ids()
        groups = sorted(list_groups(source_id=asid), key=lambda g: (0 if g.get("pinned") else 1, g.get("sort_order", 0)))
        selected = _collect_selected_channels(groups, asid)
        export_map = _build_export_tvg_map(selected)
        by_group: dict[str, dict[str, int]] = {}
        for grp, ch in selected:
            group_id = grp.get("id")
            if not group_id:
                continue
            row = by_group.setdefault(group_id, {
                "channels": 0,
                "with_programmes": 0,
                "synthetic": 0,
                "dummy": 0,
                "coverage_pct": 0,
            })
            tvg_id = (export_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
            row["channels"] += 1
            if tvg_id and counts.get(tvg_id, 0) > 0:
                row["with_programmes"] += 1
            if tvg_id and synthetic.get(tvg_id, 0) > 0:
                row["synthetic"] += 1
            if tvg_id and dummy.get(tvg_id, 0) > 0:
                row["dummy"] += 1
        for row in by_group.values():
            total = max(1, row["channels"])
            # Count real guide data PLUS channels the user deliberately gave a
            # dummy/placeholder EPG — populating dummy EPG is a deliberate "I've
            # handled this" action, so it counts toward the group's coverage %.
            # Only synthetic (schedule-inferred) is still left out. (2026-06-09)
            covered = max(0, row["with_programmes"] - row["synthetic"])
            row["coverage_pct"] = int(round((covered / total) * 100))
            row["estimated"] = False

        # Disabled groups never enter the export, so the programme index above
        # has nothing for them. Estimate their coverage instead: the share of
        # channels whose tvg_id resolves to a real guide id present in the EPG
        # sources. Flagged "estimated" so the UI can show it as approximate.
        try:
            epg_ids = {c["id"] for c in _get_epg_channels()}
        except Exception:
            epg_ids = set()
        if epg_ids:
            for grp in groups:
                if grp.get("enabled", True):
                    continue
                gid = grp.get("id")
                if not gid or gid in by_group:
                    continue
                chs, _ = get_group_channels(gid, limit=50000, source_id=asid)
                total = 0
                mapped = 0
                for ch in chs:
                    if not ch.get("enabled", True):
                        continue
                    total += 1
                    tvg = (ch.get("tvg_id") or "").strip()
                    if tvg and not _suspicious_tvg_id(tvg) and tvg in epg_ids:
                        mapped += 1
                if total:
                    by_group[gid] = {
                        "channels": total,
                        "with_programmes": mapped,
                        "synthetic": 0,
                        "dummy": 0,
                        "coverage_pct": int(round((mapped / total) * 100)),
                        "estimated": True,
                    }
        return by_group

    # Stale-while-revalidate: the editor refetches /api/groups after every
    # action, and this build parses the whole EPG XML (~18s). Serving stale
    # coverage keeps the editor responsive; the pill values just lag a little.
    return _cached_response_swr("groups:coverage", ttl_sec=300.0, build_fn=_build, fallback={})


def _refresh_xc_account_status_async(sources: list[dict], force: bool = False):
    global _xc_refresh_inflight
    xc_sources = [s for s in sources if (s.get("type") or "").lower() == "xc"]
    if not xc_sources:
        return
    with _xc_refresh_lock:
        if _xc_refresh_inflight:
            return
        _xc_refresh_inflight = True

    def _run():
        global _xc_refresh_inflight
        try:
            _refresh_xc_account_status(xc_sources, force=force)
        except Exception as exc:
            _record_backend_error("sources.refresh_account.async", exc)
        finally:
            _invalidate_response_cache(prefix="dashboard")
            with _xc_refresh_lock:
                _xc_refresh_inflight = False

    t = threading.Thread(target=_run, name="xc-account-refresh", daemon=True)
    t.start()


def _maybe_run_epg_watchdog(force: bool = False):
    """Run watchdog on a cadence, with optional force for critical refresh points."""
    global _last_epg_watchdog_run
    now = time.time()
    if not force and (now - _last_epg_watchdog_run) < _EPG_WATCHDOG_INTERVAL_SEC:
        return _epg_watchdog_status
    try:
        status = _run_epg_watchdog_check()
        _last_epg_watchdog_run = now
        return status
    except Exception as exc:
        _record_backend_error("watchdog.maybe_run", exc)
        return _epg_watchdog_status

def schedule_export(delay=2.0):
    """Schedule a background re-export after `delay` seconds. Resets on repeated calls."""
    global _export_timer
    # Editor mutations make the lineup/order health stale immediately; don't
    # wait for the debounced export to finish before invalidating those views.
    _invalidate_response_cache(prefix="groups:")
    with _export_lock:
        if _export_timer is not None:
            _export_timer.cancel()
        _export_timer = threading.Timer(delay, _do_background_export)
        _export_timer.daemon = True
        _export_timer.start()
        _export_status["status"] = "pending"

def _do_background_export():
    global _export_timer, _export_status
    try:
        _, _, count = write_exports()
        _export_status.update(status="ok", time=datetime.now(timezone.utc).isoformat(),
                              channels=count, error=None)
        _refresh_guide_cache()
    except Exception as e:
        import logging
        logging.getLogger("m3u_boss.export").error("Export failed: %s", e)
        _export_status.update(status="error", time=datetime.now(timezone.utc).isoformat(),
                              error=str(e))
    finally:
        _invalidate_response_cache()
        _prewarm_dashboard_cache_async()
    with _export_lock:
        _export_timer = None


def _serialized_export(fn):
    """Ensure only one export build runs at a time across threads."""

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        with _export_write_lock:
            return fn(*args, **kwargs)

    return _wrapped


# ═══════════════════════════════════════════════════════════════════
# Fetch helpers
# ═══════════════════════════════════════════════════════════════════

# Fetch helpers, XC/M3U importers and the rule engine now live in
# app/importers.py and app/rules.py.
from app.importers import (
    fetch_json, fetch_text, parse_server, normalize_name, _is_hdhr_source,
    build_stream_url, import_channels_from_xc, fetch_xc_account_info,
    parse_extinf_attrs, _prefix_chno, import_channels_from_m3u,
)
from app.rules import (
    matches_rule, preview_rules, apply_rules, _auto_group_by_source, _assign_new_channels,
)
from app import history as lineup_history
from app.release_features import (
    create_router as create_release_features_router,
    _validate_backup as validate_backup_data,
)


# ═══════════════════════════════════════════════════════════════════
# EPG helpers
# ═══════════════════════════════════════════════════════════════════

# parse_epg, enrich_tvg_ids, build_filtered_epg and the EPG name/call-sign
# helpers now live in app/epg.py (imported near the top of this file).

def _channel_epg_key(ch):
    return _channel_identity_key(ch.get("tvg_name") or ch.get("source_name") or ch.get("name") or "")

def _load_json_setting(key: str, fallback):
    raw = get_setting(key)
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _make_export_dummy_tvg_id(ch):
    name = ch.get("tvg_name") or ch.get("name") or ch.get("source_name") or "channel"
    slug = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")[:56].strip(".")
    if not slug:
        slug = "channel"
    cid = re.sub(r"[^a-z0-9]+", "", str(ch.get("id") or "").lower())[:10]
    if not cid:
        cid = re.sub(r"[^a-z0-9]+", "", f"{ch.get('source_id') or 0}{ch.get('stream_id') or ''}".lower())[:10]
    return f"dummy.{slug}.{cid or uuid.uuid4().hex[:8]}"


def _build_export_tvg_map(selected):
    """Build export-safe tvg ids for channels in the current export.

    Some providers reuse the same XMLTV channel id for different channel names.
    When multiple sources are active at once, that causes the merged EPG to
    cross-wire guide data between unrelated channels. Keep the original id when
    it is unambiguous, otherwise scope it to the source for export purposes.

    The scoped id is persisted per physical channel id. Without that, duplicate
    channels from the same source can flip between e.g. ``ABC.us__src2`` and
    ``ABC.us__src2_2`` when export order shifts, which looks like guide drift to
    clients even though the underlying channel numbers stayed stable.
    """
    by_tvg = {}
    for _, ch in selected:
        tvg = (ch.get("tvg_id") or "").strip()
        if tvg:
            by_tvg.setdefault(tvg, []).append(ch)

    export_map = {}
    used_export_ids = set(by_tvg.keys())
    persisted = _load_json_setting("epg_export_tvg_id_map", {})
    next_persisted = {}

    for _, ch in selected:
        if (ch.get("tvg_id") or "").strip():
            continue
        dummy = _make_export_dummy_tvg_id(ch)
        suffix = 2
        candidate = dummy
        while candidate in used_export_ids:
            candidate = f"{dummy}.{suffix}"
            suffix += 1
        export_map[ch["id"]] = candidate
        used_export_ids.add(candidate)

    for tvg, chs in by_tvg.items():
        keys = set()
        for ch in chs:
            key = _channel_epg_key(ch)
            if key:
                keys.add(key)
        if len(keys) <= 1:
            for ch in chs:
                export_map[ch["id"]] = tvg
                used_export_ids.add(tvg)
            continue

        def stable_key(c):
            cid = c.get("id", "")
            saved = persisted.get(cid, "")
            return (
                0 if saved else 1,
                saved,
                str(c.get("source_id", "")),
                str(c.get("source_order", "")),
                str(c.get("sort_order", "")),
                cid,
            )

        for ch in sorted(chs, key=stable_key):
            cid = ch["id"]
            saved = (persisted.get(cid) or "").strip()
            if saved and (saved == tvg or saved.startswith(f"{tvg}__src")) and saved not in used_export_ids:
                export_map[cid] = saved
                used_export_ids.add(saved)
                next_persisted[cid] = saved
                continue

            scoped = f"{tvg}__src{ch.get('source_id') or 0}"
            suffix = 2
            if scoped in used_export_ids:
                while f"{scoped}_{suffix}" in used_export_ids:
                    suffix += 1
                scoped = f"{scoped}_{suffix}"
            export_map[cid] = scoped
            used_export_ids.add(scoped)
            next_persisted[cid] = scoped
    if next_persisted != persisted:
        set_setting("epg_export_tvg_id_map", json.dumps(next_persisted, separators=(",", ":")))
    return export_map

def _append_epg_matches(epg_root, channels, export_tvg_map, seen_channel_ids,
                        channel_elems, programme_elems, prog_in_window):
    """Append EPG entries for channels, rewriting ids when export scoping is needed."""
    def _resolved_export_id(ch, orig_tvg):
        """Return a safe export tvg-id for the current channel tvg mapping.

        `export_tvg_map` is built before EPG auto-matching and can become stale if
        a channel's tvg_id is corrected during the same export run. Only trust the
        mapped value when it still matches the current source tvg-id namespace.
        """
        mapped = (export_tvg_map.get(ch["id"]) or "").strip()
        if not mapped:
            return orig_tvg
        if mapped == orig_tvg or mapped.startswith(f"{orig_tvg}__src"):
            return mapped
        return orig_tvg

    channel_lookup = {}
    for n in epg_root.findall("channel"):
        cid = n.attrib.get("id")
        if cid and cid not in channel_lookup:
            channel_lookup[cid] = n

    orig_to_export = {}
    export_name_by_id = {}
    orig_to_keys = {}
    for ch in channels:
        orig = (ch.get("tvg_id") or "").strip()
        if not orig or orig not in channel_lookup:
            continue
        export_id = _resolved_export_id(ch, orig).strip()
        if not export_id:
            continue
        orig_to_export.setdefault(orig, set()).add(export_id)
        fallback_name = (ch.get("tvg_name") or ch.get("name") or "").strip()
        if fallback_name and export_id not in export_name_by_id:
            export_name_by_id[export_id] = fallback_name
        key = _channel_epg_key(ch)
        if key:
            orig_to_keys.setdefault(orig, set()).add(key)

    ambiguous_orig = {orig for orig, keys in orig_to_keys.items() if len(keys) > 1}

    matched = set()
    for orig, export_ids in orig_to_export.items():
        # Don't skip ambiguous origins if they've been scoped to unique export IDs.
        # Scoping (e.g., btsport3.uk -> btsport3.uk__src2) already handles cross-wiring.
        # Only skip if there are no export_ids mapped (shouldn't happen, but be safe).
        if not export_ids:
            continue
        for export_id in export_ids:
            if export_id in seen_channel_ids:
                matched.add(export_id)
                continue
            elem = copy.deepcopy(channel_lookup[orig])
            elem.set("id", export_id)
            dn = elem.find("display-name")
            if dn is None:
                dn = ET.SubElement(elem, "display-name")
            if not (dn.text or "").strip() and export_name_by_id.get(export_id):
                dn.text = export_name_by_id[export_id]
            channel_elems.append(elem)
            seen_channel_ids.add(export_id)
            matched.add(export_id)

    for n in epg_root.findall("programme"):
        orig = n.attrib.get("channel")
        if orig not in orig_to_export or not prog_in_window(n):
            continue
        # Don't skip ambiguous origins; copy programmes to all scoped export IDs.
        for export_id in orig_to_export[orig]:
            elem = copy.deepcopy(n)
            elem.set("channel", export_id)
            fallback_name = export_name_by_id.get(export_id) or "Unknown Channel"

            # Providers can emit multiple <title>/<desc> nodes (different langs),
            # and some players pick a non-first node. Fill all blank variants.
            title_nodes = elem.findall("title")
            if not title_nodes:
                title_nodes = [ET.SubElement(elem, "title", lang="en")]
            for tnode in title_nodes:
                if not (tnode.text or "").strip():
                    tnode.text = fallback_name

            desc_nodes = elem.findall("desc")
            if not desc_nodes:
                desc_nodes = [ET.SubElement(elem, "desc", lang="en")]
            for dnode in desc_nodes:
                if not (dnode.text or "").strip():
                    dnode.text = f"{fallback_name} — guide details unavailable"
            programme_elems.append(elem)

    return matched

def _add_year_to_movie_titles(programme_elems, selected, export_tvg_map):
    """Append the release year to programme <title> for the Tunarr 24/7 Movies
    channels (e.g. 'Thunderball' -> 'Thunderball (1965)'), pulling the year from
    the XMLTV <date> element so it shows in the guide grid. Scoped to my Tunarr
    movie channels (tvg-id *.tunarr.com) in the '24/7 Movies' group — provider
    channels in that group are left untouched."""
    movie_ids = set()
    for grp, ch in selected:
        if (grp.get("name") or "").strip().lower() != "24/7 movies":
            continue
        tvg = (ch.get("tvg_id") or "").strip()
        if "tunarr.com" not in tvg:
            continue
        movie_ids.add(tvg)
        xid = export_tvg_map.get(ch["id"])
        if xid:
            movie_ids.add(xid)
    if not movie_ids:
        return
    for p in programme_elems:
        if (p.attrib.get("channel") or "").strip() not in movie_ids:
            continue
        date_node = p.find("date")
        year = (date_node.text or "").strip()[:4] if date_node is not None else ""
        if not (year.isdigit() and len(year) == 4):
            continue
        for tnode in p.findall("title"):
            t = (tnode.text or "").strip()
            if t and not t.endswith(f"({year})"):
                tnode.text = f"{t} ({year})"


def _promote_tunarr_episode_titles(programme_elems, selected, export_tvg_map):
    """Put Tunarr's episode name where IPTV guide grids can actually show it.

    Tunarr correctly emits a series name in ``<title>`` and the episode name in
    ``<sub-title>``. Several of our clients display only ``<title>``, making a
    24/7 channel look as if every time slot is merely "Friends" or "The Office".
    Promote the season/episode number and episode name into the primary title,
    while retaining ``<sub-title>`` and all descriptions for richer clients.

    Single-show 24/7 TV channels lead with the episode so narrow grids remain
    useful. Mixed movie channels (such as Jane Austen, which also contains
    miniseries) retain the series name to distinguish those episodes from films.
    """
    tunarr_channels = {}
    for grp, ch in selected:
        group_name = (grp.get("name") or "").strip().lower()
        if group_name not in {"24/7 tv", "24/7 movies"}:
            continue
        tvg = (ch.get("tvg_id") or "").strip()
        if "tunarr.com" not in tvg:
            continue
        ids = {tvg}
        export_id = (export_tvg_map.get(ch["id"]) or "").strip()
        if export_id:
            ids.add(export_id)
        for channel_id in ids:
            tunarr_channels[channel_id] = group_name

    for programme in programme_elems:
        group_name = tunarr_channels.get((programme.attrib.get("channel") or "").strip())
        if not group_name:
            continue
        subtitle = next(
            ((node.text or "").strip() for node in programme.findall("sub-title")
             if (node.text or "").strip()),
            "",
        )
        if not subtitle:
            continue
        episode_number = next(
            ((node.text or "").strip() for node in programme.findall("episode-num")
             if (node.text or "").strip()),
            "",
        )
        for title_node in programme.findall("title"):
            series = (title_node.text or "").strip()
            details = " · ".join(part for part in (episode_number, subtitle) if part)
            if group_name == "24/7 movies" and series:
                title_node.text = f"{series} · {details}"
            else:
                title_node.text = details

def _make_dummy_tvg_id(name):
    slug = re.sub(r"[^a-z0-9]+", ".", name.lower()).strip(".")
    return f"dummy.{slug}" if slug else f"dummy.{uuid.uuid4().hex[:8]}"


def _is_placeholder_programme_title(title):
    text = (title or "").strip().lower()
    if not text:
        return True
    return text in {"signing off", "no event today", "off air", "to be announced", "tba"}


def _sanitize_xml_text(value: str | None) -> str | None:
    """Strip characters that are illegal in XML 1.0 text nodes."""
    if value is None:
        return None
    out = []
    for ch in value:
        cp = ord(ch)
        if cp in (0x9, 0xA, 0xD) or (0x20 <= cp <= 0xD7FF) or (0xE000 <= cp <= 0xFFFD) or (0x10000 <= cp <= 0x10FFFF):
            out.append(ch)
    return "".join(out)


def _sanitize_xml_tree(root_elem):
    """Clean illegal XML characters from all element text/tail in-place."""
    for elem in root_elem.iter():
        elem.text = _sanitize_xml_text(elem.text)
        elem.tail = _sanitize_xml_text(elem.tail)
        for k, v in list(elem.attrib.items()):
            elem.attrib[k] = _sanitize_xml_text(v) or ""


def _normalize_programme_times(root_elem):
    """Rewrite every <programme> start/stop timestamp to UTC (+0000).

    Source EPGs emit a mix of timezone offsets (e.g. +0100 from the IPTVBoss
    feed, -0400, +0000). Several IPTV players (TiviMate, Televizio, ...)
    mishandle non-local offsets and shift the whole guide by hours — that is
    what made KDKA's 7AM news land in a mid-afternoon slot. Converting every
    timestamp to one UTC offset preserves the exact instant while removing
    anything a player can misread.
    """
    def _to_utc(value):
        s = (value or "").strip()
        m = re.match(r"^(\d{14})\s*([+-]\d{4})?$", s)
        if not m:
            return value
        digits, offset = m.group(1), m.group(2)
        try:
            dt = datetime.strptime(digits, "%Y%m%d%H%M%S")
        except ValueError:
            return value
        if offset:
            sign = 1 if offset[0] == "+" else -1
            dt = dt - sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        return dt.strftime("%Y%m%d%H%M%S") + " +0000"

    for prog in root_elem.iter("programme"):
        for attr in ("start", "stop"):
            if attr in prog.attrib:
                prog.attrib[attr] = _to_utc(prog.attrib[attr])


_EVENTY_NAME_RE = re.compile(
    r"\b(f1|formula\s*1|ppv|pay\s*per\s*view|sprint|qualifying|practice|race|warm\s*up|event)\b"
    r"|\(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\)"
    r"|\b\d{4}-\d{2}-\d{2}\b",
    re.IGNORECASE,
)

# _LOOP_CHANNEL_RE / _channel_is_loop_like moved to app/epg.py (imported above).


def _channel_should_avoid_fallback_match(ch):
    """Return True for channels better served by event/name dummy fallback.

    These channels tend to have ephemeral labels and poor cross-provider
    matching quality, so borrowing tvg ids often creates wrong EPG mappings.
    """
    group_name = (ch.get("source_group") or "").lower()
    name = (ch.get("tvg_name") or ch.get("source_name") or ch.get("name") or "")
    if _EVENTY_NAME_RE.search(name or ""):
        return True
    if "ppv" in group_name:
        return True
    if "sports" in group_name and ("event" in group_name or "apple" in group_name or "f1" in group_name):
        return True
    if _channel_is_loop_like(ch):
        return True
    return False


def _fill_blank_programme_text(channel_elems, programme_elems):
    """Ensure exported programme rows always have at least channel-name text."""
    channel_names = {}
    for ce in channel_elems:
        cid = (ce.attrib.get("id") or "").strip()
        if not cid:
            continue
        dn = ce.find("display-name")
        nm = ((dn.text or "") if dn is not None else "").strip()
        if nm:
            channel_names[cid] = nm

    for pe in programme_elems:
        cid = (pe.attrib.get("channel") or "").strip()
        fallback = channel_names.get(cid) or "Unknown Channel"
        title_nodes = pe.findall("title")
        if not title_nodes:
            title_nodes = [ET.SubElement(pe, "title", lang="en")]
        for tnode in title_nodes:
            if not (tnode.text or "").strip():
                tnode.text = fallback

        desc_nodes = pe.findall("desc")
        if not desc_nodes:
            desc_nodes = [ET.SubElement(pe, "desc", lang="en")]
        for dnode in desc_nodes:
            if not (dnode.text or "").strip():
                dnode.text = f"{fallback} — guide details unavailable"


def _event_title_from_channel_name(ch):
    """Extract a human-friendly event title from a channel name."""
    raw = (ch.get("tvg_name") or ch.get("name") or ch.get("source_name") or "").strip()
    if not raw:
        return ""
    title = re.sub(r'\s*@\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{1,2}:\d{2}\s*[AP]M\s+[A-Z]{2}\s*$', '', raw)
    title = re.sub(r'\s*\((?:\d{1,2}\.\d{1,2}\s+\d{1,2}:\d{2}\s*[AP]M\s+[A-Z]{2}|\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\)\s*$', '', title)
    parts = re.split(r'\s*:\s*', title, maxsplit=1)
    if len(parts) == 2 and _extract_slot(parts[0]):
        title = parts[1]
    return title.strip(" :-")


def add_event_epg_entries(selected, export_tvg_map, channel_elems, programme_elems):
    """Generate synthetic guide rows for timed event channels missing usable guide data.

    This is mainly for sports/event slot channels where the provider updates the
    channel name before XMLTV programme rows catch up.
    """
    now = datetime.now(timezone.utc)
    fmt = "%Y%m%d%H%M%S %z"
    channel_lookup = {}
    for elem in channel_elems:
        cid = (elem.attrib.get("id") or "").strip()
        if cid:
            channel_lookup[cid] = elem

    programme_titles = {}
    for elem in programme_elems:
        cid = (elem.attrib.get("channel") or "").strip()
        if not cid:
            continue
        title_el = elem.find("title")
        title = (title_el.text or "") if title_el is not None else ""
        programme_titles.setdefault(cid, []).append(title)

    synthetic_channels = []
    synthetic_programmes = []
    for _, ch in selected:
        export_id = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
        if not export_id:
            continue

        existing_titles = programme_titles.get(export_id, [])
        if existing_titles and not all(_is_placeholder_programme_title(t) for t in existing_titles):
            continue

        start = _parse_channel_event_start(ch.get("name") or ch.get("tvg_name") or "", now)
        if start is None:
            continue
        if start < now - timedelta(hours=6) or start > now + timedelta(hours=48):
            continue

        title = _event_title_from_channel_name(ch) or (ch.get("name") or "Unnamed")
        stop = start + timedelta(hours=3)

        channel_elem = channel_lookup.get(export_id)
        if channel_elem is None:
            channel_elem = ET.Element("channel", id=export_id)
            channel_lookup[export_id] = channel_elem
            synthetic_channels.append(channel_elem)

        display_names = channel_elem.findall("display-name")
        if not display_names:
            dn = ET.SubElement(channel_elem, "display-name")
            dn.text = title
        elif not any((dn.text or "").strip() for dn in display_names):
            display_names[0].text = title

        if not existing_titles:
            logo = (ch.get("logo") or "").strip()
            if logo and channel_elem.find("icon") is None:
                ET.SubElement(channel_elem, "icon", src=logo)

        prog = ET.Element("programme", start=start.strftime(fmt), stop=stop.strftime(fmt), channel=export_id)
        title_el = ET.SubElement(prog, "title", lang="en")
        title_el.text = title
        desc_el = ET.SubElement(prog, "desc", lang="en")
        desc_el.text = f"{title} — schedule inferred from channel metadata"
        synthetic_programmes.append(prog)

    return synthetic_channels, synthetic_programmes

def add_dummy_epg_entries(selected, matched, dur=120, export_tvg_map=None):
    """Generate placeholder EPG for channels without real guide data.

    Returns (channel_elems, programme_elems) so the caller can maintain
    strict XMLTV element ordering (all channels before all programmes),
    which is required by players like TiviMate and Televizio.
    """
    export_tvg_map = export_tvg_map or {}
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y%m%d%H%M%S %z"
    dummy_channels = []
    dummy_programmes = []
    for grp, ch in selected:
        tvg = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
        if tvg and tvg in matched: continue
        if not tvg:
            tvg = _make_dummy_tvg_id(ch.get("name") or "channel")
        if tvg in matched: continue
        matched.add(tvg)
        display = ch.get("name") or "Unnamed"
        ce = ET.Element("channel", id=tvg)
        dn = ET.SubElement(ce, "display-name"); dn.text = display
        logo = (ch.get("logo") or "").strip()
        if logo: ET.SubElement(ce, "icon", src=logo)
        dummy_channels.append(ce)
        slot = now; end = now + timedelta(hours=48); delta = timedelta(minutes=dur)
        while slot < end:
            se = slot + delta
            p = ET.Element("programme", start=slot.strftime(fmt), stop=se.strftime(fmt), channel=tvg)
            t = ET.SubElement(p, "title", lang="en"); t.text = display
            d = ET.SubElement(p, "desc", lang="en"); d.text = f"{display} — no guide data"
            dummy_programmes.append(p)
            slot = se
    return dummy_channels, dummy_programmes


# ═══════════════════════════════════════════════════════════════════
# Export
# ═══════════════════════════════════════════════════════════════════

def _collect_selected_channels(ordered_groups, asid):
    selected = []
    for grp in ordered_groups:
        if not grp.get("enabled", True):
            continue
        chs, _ = get_group_channels(grp["id"], limit=50000, source_id=asid)
        for ch in chs:
            if ch.get("enabled", True):
                selected.append((grp, ch))
    return selected


def _atomic_write_bytes(path: Path, payload: bytes):
    """Write bytes via temp file + replace to avoid partial/corrupt reads."""
    tmp_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    tmp_path = path.with_name(tmp_name)
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _write_sibling_gz(path: Path):
    """(Re)generate path.gz from path via streaming copy so the edge/app can
    always serve the pre-compressed file and never has to gzip the 111MB EPG
    on the fly. Written atomically (temp + replace) so readers never see a
    partial .gz. Best-effort: failures here must not break the export."""
    gz_path = path.with_name(path.name + ".gz")
    gz_tmp = gz_path.with_suffix(gz_path.suffix + ".tmp")
    try:
        import shutil as _sh
        with open(path, "rb") as f_in, gzip.open(gz_tmp, "wb", compresslevel=1) as f_out:
            _sh.copyfileobj(f_in, f_out, 65536)
        os.replace(gz_tmp, gz_path)
    except Exception:
        try:
            if gz_tmp.exists():
                gz_tmp.unlink()
        except OSError:
            pass


def _atomic_write_xml(path: Path, root: ET.Element):
    # Stream straight to a temp file then rename. Avoids holding two copies of
    # the serialized output (BytesIO + .getvalue() bytes) in memory — for a
    # 111MB EPG that's ~220MB of resident memory just for the write step.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        ET.ElementTree(root).write(f, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, path)
    # Keep the pre-gzipped sibling in lock-step with the raw file so it can
    # never go stale/missing relative to organized.xml — otherwise the edge
    # falls back to shipping the full 111MB raw EPG to every client.
    if path.name == "organized.xml":
        _write_sibling_gz(path)


def _build_epg_icon_map(epg_path):
    """Read the merged XMLTV and return {tvg_id: icon_url} for every channel
    that has at least one <icon src=...> child. iterparse + elem.clear keeps
    the memory footprint flat even for the 100MB+ EPGs we ship.
    """
    if not Path(epg_path).exists():
        return {}
    icons = {}
    try:
        for _event, elem in ET.iterparse(str(epg_path), events=("end",)):
            tag = elem.tag.split("}", 1)[-1]
            if tag == "channel":
                cid = (elem.attrib.get("id") or "").strip()
                if cid and cid not in icons:
                    icon_node = elem.find("icon")
                    if icon_node is not None:
                        src = (icon_node.attrib.get("src") or "").strip()
                        if src:
                            icons[cid] = src
                elem.clear()
            elif tag == "programme":
                # No icons of interest under <programme>; drop to save memory.
                elem.clear()
    except Exception:
        return icons
    return icons


def _write_m3u_export(selected, ordered_groups, hdhr_sids, export_tvg_map, epg_icon_map=None):
    """Write the organized M3U.

    `epg_icon_map` is `{tvg_id: icon_url}` harvested from the merged XMLTV.
    Channels whose upstream M3U entry has no `tvg-logo` will fall back to
    the EPG-supplied icon for their tvg_id — that's what gives e.g. antenna
    channels their network logos in players when the source feed omits them.
    """
    epg_icon_map = epg_icon_map or {}
    lines = [
        "#EXTM3U",
        f"# Exported by M3U Boss — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"# {len(selected)} channels in {sum(1 for g in ordered_groups if g.get('enabled', True))} groups",
    ]

    def _group_tag(name: str) -> str:
        words = re.sub(r"[^a-zA-Z0-9 ]", "", name).split()
        if not words:
            return ""
        if len(words) == 1:
            return words[0][:3].upper()
        if len(words) >= 3:
            return "".join(w[0] for w in words).upper()
        return words[0][:3].upper()

    # Stable, persistent per-channel numbers (see db.assign_export_chnos):
    # a channel keeps its tvg-chno across exports even as the lineup churns, so
    # XC players (TiviMate/Televizo) that cache the channel list never drift
    # their guide onto the wrong channel. Seeded from the current export order,
    # so existing channels keep the numbers they already have.
    chno_by_id = assign_export_chnos([ch["id"] for _, ch in selected])
    for grp, ch in selected:
        tvg_id = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").replace('"',"")
        tvg_name = (ch.get("tvg_name") or ch.get("name") or "").replace('"',"")
        logo = (ch.get("logo") or "").replace('"',"")
        # Fallback: if the M3U source didn't include a logo, use the EPG's
        # <icon src=...> for this tvg_id. Mostly hits antenna / OTA channels
        # whose upstream HDHR feeds don't carry network logos.
        if not logo and tvg_id:
            epg_logo = (epg_icon_map.get(tvg_id) or "").replace('"', "")
            if epg_logo:
                logo = epg_logo
        # Single-game event channels (name IS one matchup, e.g.
        # "MLB 01 | Marlins at Pirates …") → use the matchup composite as the
        # CHANNEL logo so it shows in the guide GRID, not just program-info.
        # Only fires when the name resolves to a real matchup; multi-game
        # channels (ESPN, Sky, team channels) are left untouched. Re-evaluated
        # every export, so it tracks each day's game automatically.
        ml = _match_logo_url_for_channel(ch.get("name") or "", tvg_id, grp.get("name") or "")
        if ml:
            logo = ml
        gname = (grp.get("name") or "").replace('"',"")
        display = ch.get("name") or "Unnamed"
        if ch.get("smart_group_id"):
            custom_tag = (grp.get("export_tag") or "").strip()
            tag = custom_tag if custom_tag else _group_tag(gname)
            if tag:
                display = f"{display} [{tag}]"
                tvg_name = f"{tvg_name} [{tag}]"
        src_chno = (ch.get("channel_number") or "").strip()
        if src_chno and ch.get("source_id") in hdhr_sids and not tvg_name.startswith(src_chno):
            tvg_name = f"{src_chno} {tvg_name}"
        chno = str(chno_by_id.get(ch["id"], ""))
        attrs = []
        if tvg_id:
            attrs.append(f'tvg-id="{tvg_id}"')
        attrs.append(f'tvg-name="{tvg_name}"')
        if chno:
            attrs.append(f'tvg-chno="{chno}"')
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        attrs.append(f'group-title="{gname}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{display}')
        if gname:
            lines.append(f'#EXTGRP:{gname}')
        url = _rewrite_local_stream_url(ch.get("url") or "")
        if ch.get("smart_group_id") and url:
            sep = "&" if "?" in url else "?"
            tag = _group_tag(gname).lower() or "sg"
            url = f"{url}{sep}sg={tag}"
        lines.append(url)

    m3u_path = EXPORT_DIR / "organized.m3u"
    _atomic_write_bytes(m3u_path, ("\n".join(lines) + "\n").encode("utf-8"))
    return m3u_path


def _inject_match_logos(root):
    """Add a combined matchup-team <icon> to sports <programme> elements whose
    title parses as a recognised league matchup (NFL/NBA/MLB/NHL/WNBA/MLS/World
    Cup/UFL/WHL…). Best-effort: NEVER raises; skips cleanly when a team can't be
    resolved (no logo beats a wrong logo). TiviMate renders these in the guide.
    Composites are built once per matchup and cached, so steady-state runs do
    ~zero network/CPU work here.
    """
    try:
        from app import match_logos
    except Exception:
        return 0
    try:
        base = (get_setting("match_logo_base_url")
                or os.environ.get("M3U_BOSS_PUBLIC_URL")
                or "http://localhost:43817").rstrip("/")
        match_logos.reset_run_budget()
        seen: dict[tuple, str | None] = {}  # (title,hint) -> filename|None
        added = 0
        unresolved: list[dict] = []   # parsed as a matchup but a team didn't resolve
        for prog in root.findall("programme"):
            if prog.find("icon") is not None:
                continue  # don't clobber existing programme art
            t = prog.find("title")
            title = (t.text or "").strip() if (t is not None and t.text) else ""
            if not title:
                continue
            # Infer the sport from the channel id for event channels whose titles
            # omit the league word (e.g. "Marlins at Pirates" on BossSports.MLB.*).
            hint = _channel_sport_hint(prog.attrib.get("channel") or "")
            key = (title, hint)
            if key not in seen:
                fname = match_logos.match_logo_filename(title, hint)
                seen[key] = fname
                # Classify near-misses for the coverage report. Cache is warm
                # from the resolve above, so this is cheap (no extra network).
                if not fname:
                    diag = match_logos.match_logo_diagnose(title, hint)
                    if diag.get("status") in ("unresolved", "error") and len(unresolved) < 200:
                        unresolved.append(diag)
            fname = seen[key]
            if fname:
                ET.SubElement(prog, "icon", src=f"{base}/logos/{fname}")
                added += 1
        resolved_titles = sum(1 for v in seen.values() if v)
        with _logo_coverage_lock:
            _logo_coverage.clear()
            _logo_coverage.update({
                "at": datetime.now(timezone.utc).isoformat(),
                "distinct_titles": len(seen),
                "resolved_titles": resolved_titles,
                "programmes_tagged": added,
                "unresolved_count": len(unresolved),
                "unresolved_sample": unresolved,
            })
        return added
    except Exception:
        return 0


# Channel-id → sport, for event channels whose programme titles omit the league
# word. Covers the BossSports.<LEAGUE>.NN event-channel ids and common patterns.
_CH_SPORT_PATTERNS = [
    (re.compile(r"\bWNBA\b|\bNBA\b|basketball", re.I), "Basketball"),
    (re.compile(r"\bMLB\b|baseball", re.I), "Baseball"),
    (re.compile(r"\bNFL\b|\bUFL\b|\bCFL\b|\bXFL\b", re.I), "American Football"),
    (re.compile(r"\bNHL\b|\bAHL\b|\bWHL\b|\bOHL\b|hockey", re.I), "Ice Hockey"),
    (re.compile(r"\bMLS\b|\bEPL\b|\bEFL\b|la\s?liga|serie\s?a|bundesliga"
                r"|ligue\s?1|\bUEFA\b|champions league|soccer|futbol|f[úu]tbol", re.I), "Soccer"),
]


def _channel_sport_hint(channel_id: str):
    cid = channel_id or ""
    for pat, sport in _CH_SPORT_PATTERNS:
        if pat.search(cid):
            return sport
    return None


def _match_logo_url_for_channel(name, tvg_id, group_name):
    """Composite-logo URL if this channel's NAME is a single matchup (event
    channel), else None. Sport hint comes from the tvg-id or the group name
    ("Sports | MLB"). Reuses match_logos' cache → no extra network. Never raises."""
    if not name:
        return None
    try:
        from app import match_logos
    except Exception:
        return None
    try:
        hint = _channel_sport_hint(tvg_id) or _channel_sport_hint(group_name)
        fname = match_logos.match_logo_filename(name, hint)
        if not fname:
            return None
        base = (get_setting("match_logo_base_url")
                or os.environ.get("M3U_BOSS_PUBLIC_URL")
                or "http://localhost:43817").rstrip("/")
        return f"{base}/logos/{fname}"
    except Exception:
        return None


@_serialized_export
def write_exports():
    asid = get_active_source_ids()

    # Clear stale dummy tvg_ids so channels get a fresh chance at real EPG matching
    clear_dummy_tvg_ids()

    channels = get_all_channels_raw(source_id=asid)
    if not channels:
        raise HTTPException(400, "No channels to export")

    groups = list_groups(source_id=asid)
    groups_map = {g["id"]: g for g in groups}

    # Collect enabled in group order
    ordered_groups = sorted(groups, key=lambda g: (0 if g.get("pinned") else 1, g.get("sort_order",0)))
    hdhr_sids = {s["id"] for s in get_active_sources() if _is_hdhr_source(s.get("name",""))}
    selected = _collect_selected_channels(ordered_groups, asid)

    # Avoid guide collisions when different active sources reuse the same tvg-id
    # for different channel names.
    export_tvg_map = _build_export_tvg_map(selected)

    # Build set of tvg_ids actually in the M3U export — only these need EPG data
    exported_tvg_ids = set()
    for _, ch in selected:
        tvg = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
        if tvg:
            exported_tvg_ids.add(tvg)

    # EPG time window: configurable via settings (default 4 days)
    try:
        # Clamp DAYS to 1..14 first, then convert to hours (clamping the day
        # value against hour bounds silently forced a 24-day window).
        _epg_window_hours = max(1, min(14, int(get_setting("epg_window_days") or 4))) * 24
    except (TypeError, ValueError):
        _epg_window_hours = 96
    _epg_now = datetime.now(timezone.utc)
    _epg_start = _epg_now - timedelta(hours=6)
    _epg_cutoff = _epg_now + timedelta(hours=_epg_window_hours)
    _epg_fmt = "%Y%m%d%H%M%S %z"
    _epg_fmt_alt = "%Y%m%d%H%M%S"

    def _prog_in_window(prog_elem):
        """Return True if programme overlaps the export time window."""
        start_str = prog_elem.attrib.get("start", "")
        stop_str = prog_elem.attrib.get("stop", "")
        try:
            start = datetime.strptime(start_str, _epg_fmt)
        except ValueError:
            try:
                start = datetime.strptime(start_str[:14], _epg_fmt_alt).replace(tzinfo=timezone.utc)
            except ValueError:
                return True  # keep if unparseable
        # Programme must start before cutoff
        if start > _epg_cutoff:
            return False
        # Programme must not have ended before window start
        if stop_str:
            try:
                stop = datetime.strptime(stop_str, _epg_fmt)
            except ValueError:
                try:
                    stop = datetime.strptime(stop_str[:14], _epg_fmt_alt).replace(tzinfo=timezone.utc)
                except ValueError:
                    return True
            if stop < _epg_start:
                return False
        return True

    # EPG — Provider-first priority: match each source's channels against
    # its own EPG first, then try additional EPGs only for still-unmatched.
    epg_path = EXPORT_DIR / "organized.xml"
    m3u_path_existing = EXPORT_DIR / "organized.m3u"
    try:
        import shutil as _shutil
        if epg_path.exists():
            _shutil.copy2(epg_path, EXPORT_DIR / "organized.xml.prev")
        if m3u_path_existing.exists():
            _shutil.copy2(m3u_path_existing, EXPORT_DIR / "organized.m3u.prev")
    except Exception:
        pass

    # Phase 1: Collect provider EPG URLs mapped to their source IDs
    provider_epgs = []  # [(url, source_id), ...]
    seen_urls = set()
    provider_epg_source_ids = set()
    for src in get_active_sources():
        u = (src.get("epg_url") or "").strip()
        if u and u not in seen_urls:
            provider_epgs.append((u, src["id"]))
            provider_epg_source_ids.add(src["id"])
            seen_urls.add(u)

    # Phase 2: Additional EPG sources (fallback only)
    fallback_epgs = []
    for es in list_epg_sources():
        if es.get("url") and es["url"] not in seen_urls:
            fallback_epgs.append(es["url"])
            seen_urls.add(es["url"])
    # EPG source priority for the fallback phase. epg.pw ships time-shifted
    # OTA/antenna listings, so it must be the last resort — any other guide
    # (e.g. EPGenus, which carries correct call-sign ".us" ids and correct
    # local times) should get to match a channel first. iptv-org guides, when
    # reachable, are well-formed so they go first. sort() is stable, so
    # insertion order is preserved within each tier.
    def _epg_rank(u):
        if "iptv-org.github.io" in u:
            return 0
        if "epg.pw" in u:
            return 2
        return 1
    fallback_epgs.sort(key=_epg_rank)

    all_epg_urls = [u for u, _ in provider_epgs] + fallback_epgs

    if all_epg_urls:
        try:
            merged_root = ET.Element("tv", {
                "generator-info-name": "M3U Boss",
                "generator-info-url": "",
            })
            all_selected = set()
            _seen_channel_ids = set()  # Deduplicate <channel> elements
            _channel_elems = []   # Collect all <channel> elements
            _programme_elems = [] # Collect all <programme> elements
            # Only build EPG for channels that will actually be exported (enabled
            # channels in enabled groups). Phase 1 used to match EVERY channel in a
            # provider source — including DISABLED ones — which bloated the EPG by
            # ~3k channels dispatcharr then had to parse (pegging it). (2026-06-09)
            selected_ids = {ch["id"] for _, ch in selected}

            # Phase 1: Provider EPGs — only match channels from that provider
            _epg_cache = {}  # url -> parsed root (avoid re-fetching in Phase 3)
            for url, sid in provider_epgs:
                try:
                    root = parse_epg(url)
                    _epg_cache[url] = root
                    _record_epg_source_ok(url, channels=len(root.findall("channel")),
                                          programmes=len(root.findall("programme")))
                    provider_channels = [ch for ch in channels if ch.get("source_id") == sid and ch["id"] in selected_ids]
                    enrich_tvg_ids(provider_channels, root)
                    matched = _append_epg_matches(
                        root, provider_channels, export_tvg_map, _seen_channel_ids,
                        _channel_elems, _programme_elems, _prog_in_window
                    )
                    all_selected.update(matched)
                except Exception as exc:
                    # One bad provider EPG must not sink the whole export — skip
                    # it but record which url failed so it's visible in the
                    # backend-errors / EPG diagnostics, not silently dropped.
                    _record_backend_error(f"epg.provider.{sid}", exc)
                    _record_epg_source_error(url, exc)
                    continue

            # Phase 2: Fallback EPGs — try channels still missing matched XMLTV data.
            # Re-read channels to get updated tvg_ids from Phase 1.
            # Include channels whose current export tvg-id still has no matched
            # XMLTV data, not just channels with an empty tvg_id.
            if fallback_epgs:
                channels = get_all_channels_raw(source_id=asid)
                selected_now = _collect_selected_channels(ordered_groups, asid)
                unmatched = []
                seen_unmatched_ids = set()
                for _, ch in selected_now:
                    # Event-style channels are poor candidates for cross-provider
                    # tvg-id matching and should prefer event/name-based fallback.
                    if _channel_should_avoid_fallback_match(ch):
                        continue
                    export_id = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
                    if export_id and export_id in all_selected:
                        continue
                    if ch["id"] in seen_unmatched_ids:
                        continue
                    unmatched.append(ch)
                    seen_unmatched_ids.add(ch["id"])
                # A channel counts as matched once an export id derived from its
                # (possibly just-corrected) tvg_id is in all_selected. Mirrors
                # _resolved_export_id so a stale export_tvg_map entry — built
                # before EPG auto-matching — can't mask a fresh match.
                def _epg_matched(ch):
                    tvg = (ch.get("tvg_id") or "").strip()
                    if not tvg:
                        return False
                    mapped = (export_tvg_map.get(ch["id"]) or "").strip()
                    eid = mapped if (mapped and (mapped == tvg
                          or mapped.startswith(f"{tvg}__src"))) else tvg
                    return eid in all_selected

                if unmatched:
                    for url in fallback_epgs:
                        if not unmatched:
                            break
                        try:
                            root = parse_epg(url)
                            _record_epg_source_ok(url, channels=len(root.findall("channel")),
                                                  programmes=len(root.findall("programme")))
                            enrich_tvg_ids(unmatched, root, require_nonblank_programmes=True)
                            matched = _append_epg_matches(
                                root, unmatched, export_tvg_map, _seen_channel_ids,
                                _channel_elems, _programme_elems, _prog_in_window
                            )
                            all_selected.update(matched)
                            # First good guide wins: drop channels that now have
                            # matched EPG data so a lower-quality guide later in
                            # the list (e.g. epg.pw, with time-shifted OTA
                            # listings) can't overwrite them.
                            unmatched = [ch for ch in unmatched if not _epg_matched(ch)]
                        except Exception as exc:
                            _record_backend_error("epg.fallback", exc)
                            _record_epg_source_error(url, exc)
                            continue
                        # Remove channels now matched in exports before trying the next fallback.
                        channels = get_all_channels_raw(source_id=asid)
                        selected_now = _collect_selected_channels(ordered_groups, asid)
                        refreshed = []
                        for _, ch in selected_now:
                            if _channel_should_avoid_fallback_match(ch):
                                continue
                            export_id = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
                            if not export_id or export_id not in all_selected:
                                refreshed.append(ch)
                        unmatched = refreshed
                        if not unmatched:
                            break

            # Refresh selected channels after EPG matching so the XML and M3U
            # use the same final tvg_ids.
            selected = _collect_selected_channels(ordered_groups, asid)
            export_tvg_map = _build_export_tvg_map(selected)

            # Fill blank event guides from channel metadata before falling back to dummy blocks.
            event_ch, event_pr = add_event_epg_entries(selected, export_tvg_map, _channel_elems, _programme_elems)

            # Coverage for dummy fallback must be based on actual programme rows,
            # not merely on whether a channel node exists in XMLTV.
            covered_ids = {
                (p.attrib.get("channel") or "").strip()
                for p in _programme_elems
                if (p.attrib.get("channel") or "").strip()
            }
            covered_ids.update(
                {
                    (p.attrib.get("channel") or "").strip()
                    for p in event_pr
                    if (p.attrib.get("channel") or "").strip()
                }
            )

            # Record EPG match coverage for the diagnostics endpoint: a channel
            # is "matched" when its export id has real programme rows in
            # covered_ids; otherwise it only gets dummy/event filler.
            try:
                _capture_epg_match_stats(selected, export_tvg_map, covered_ids)
            except Exception as exc:
                _record_backend_error("epg.match_stats", exc)

            # Append all channels first, then all programmes (XMLTV compliance)
            dummy_ch, dummy_pr = add_dummy_epg_entries(selected, covered_ids, export_tvg_map=export_tvg_map)
            for elem in _channel_elems:
                merged_root.append(elem)
            for elem in event_ch:
                merged_root.append(elem)
            for elem in dummy_ch:
                merged_root.append(elem)
            _promote_tunarr_episode_titles(_programme_elems, selected, export_tvg_map)
            _add_year_to_movie_titles(_programme_elems, selected, export_tvg_map)
            for elem in _programme_elems:
                merged_root.append(elem)
            for elem in event_pr:
                merged_root.append(elem)
            for elem in dummy_pr:
                merged_root.append(elem)
            _fill_blank_programme_text(_channel_elems + event_ch + dummy_ch, _programme_elems + event_pr + dummy_pr)
            _normalize_programme_times(merged_root)
            _sanitize_xml_tree(merged_root)
            _inject_match_logos(merged_root)
            _atomic_write_xml(epg_path, merged_root)
        except Exception as exc:
            # The XML writer is atomic, so the previous guide is still valid.
            # Never replace it with an empty <tv> document after a transient
            # provider/parser failure; surface the failure and keep live TV on
            # the last known-good export.
            _record_backend_error("export.epg_build", exc)
            raise
    else:
        dummy = ET.Element("tv", {
            "generator-info-name": "M3U Boss",
            "generator-info-url": "",
        })
        selected = _collect_selected_channels(ordered_groups, asid)
        export_tvg_map = _build_export_tvg_map(selected)
        dummy_ch, dummy_pr = add_dummy_epg_entries(selected, set(), export_tvg_map=export_tvg_map)
        for elem in dummy_ch:
            dummy.append(elem)
        for elem in dummy_pr:
            dummy.append(elem)
        _normalize_programme_times(dummy)
        _sanitize_xml_tree(dummy)
        _atomic_write_xml(epg_path, dummy)

    # Rebuild the M3U after EPG matching so guide ids stay in sync with the
    # XMLTV export and players like TiviMate do not see stale mappings.
    selected = _collect_selected_channels(ordered_groups, asid)
    export_tvg_map = _build_export_tvg_map(selected)

    # Harvest icon URLs from the just-written XMLTV so any channel whose
    # upstream M3U is missing tvg-logo can fall back to the guide's icon.
    # Reading the file we just wrote (rather than holding merged_root in
    # scope) keeps this independent of which code path produced the EPG —
    # whether the real merge, the dummy stub, or the legacy empty file.
    epg_icon_map = _build_epg_icon_map(epg_path)
    m3u_path = _write_m3u_export(selected, ordered_groups, hdhr_sids, export_tvg_map, epg_icon_map)

    # Pre-compress both exports so they can be served instantly without
    # blocking the server with real-time gzip compression on large files.
    # Use fast compression: IPTV clients benefit more from low export CPU than
    # from squeezing a few extra MB out of the XMLTV file.
    # Stream via copyfileobj — avoids loading the entire 111MB EPG into RAM
    # just to gzip it (was a ~220MB memory spike per regen otherwise).
    import shutil as _sh
    try:
        gz_m3u = EXPORT_DIR / "organized.m3u.gz"
        with open(m3u_path, "rb") as f_in, gzip.open(gz_m3u, "wb", compresslevel=1) as f_out:
            _sh.copyfileobj(f_in, f_out, 65536)
    except Exception:
        pass
    try:
        gz_epg = EXPORT_DIR / "organized.xml.gz"
        with open(epg_path, "rb") as f_in, gzip.open(gz_epg, "wb", compresslevel=1) as f_out:
            _sh.copyfileobj(f_in, f_out, 65536)
    except Exception:
        pass

    # Record export to history
    try:
        m3u_sz = m3u_path.stat().st_size
        xml_sz = epg_path.stat().st_size
        enabled_groups = sum(1 for g in ordered_groups if g.get("enabled", True))
        add_export_history(len(selected), enabled_groups, m3u_sz, xml_sz)
    except Exception:
        pass

    # Copy exports to project root for easy inspection
    import shutil
    try:
        shutil.copy2(m3u_path, BASE_DIR / "organized.m3u")
        shutil.copy2(epg_path, BASE_DIR / "organized.xml")
    except Exception:
        pass

    _invalidate_response_cache()
    # Only consume a planned-renumber marker after the newly numbered M3U is
    # fully written. A watchdog may run while the export is still pending and
    # must not baseline the previous playlist (which would create a false
    # mass-drift alert as soon as this export completes).
    try:
        _check_chno_drift(planned_export_complete=True)
    except Exception as exc:
        _record_backend_error("export.chno_rebaseline", exc)
    # EPG generation allocates a multi-GB DOM/buffer briefly. Release those
    # pages back to the OS so steady-state memory doesn't sit at the peak.
    import gc as _gc
    _gc.collect()
    _release_freed_memory()
    # Opt-in: nudge Dispatcharr to pull the fresh export immediately (no-op
    # unless configured). Background thread — never delays the export return.
    _maybe_refresh_dispatcharr()
    return m3u_path, epg_path, len(selected)


_epg_snapshot_lock = threading.Lock()
_epg_snapshot: dict[str, Any] | None = None
_epg_count_cache: dict[tuple[int, int], dict[str, int]] = {}
_epg_diff_cache: dict[str, Any] = {}


def _file_signature(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        return st.st_mtime_ns, st.st_size
    except OSError:
        return 0, 0


def _export_signature() -> tuple[tuple[int, int], tuple[int, int]]:
    return (_file_signature(EXPORT_DIR / "organized.m3u"),
            _file_signature(EXPORT_DIR / "organized.xml"))


def _parse_xmltv_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S %z")
    except ValueError:
        try:
            return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _build_epg_snapshot(signature) -> dict[str, Any]:
    """Parse the current M3U/XMLTV pair once and share the result everywhere.

    The snapshot is immutable by convention and keyed to file mtime+size, so it
    remains hot indefinitely while the export is unchanged and invalidates
    immediately after an atomic export replacement.
    """
    m3u_path = EXPORT_DIR / "organized.m3u"
    xml_path = EXPORT_DIR / "organized.xml"
    entries = []
    ids: dict[str, dict[str, str]] = {}
    tvg_names: dict[str, set[str]] = {}
    if m3u_path.exists():
        for line in m3u_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("#EXTINF"):
                continue
            attrs, display = parse_extinf_attrs(line)
            item = {
                "name": display or attrs.get("tvg-name") or "Unnamed",
                "group": attrs.get("group-title") or "Ungrouped",
                "tvg_id": (attrs.get("tvg-id") or "").strip(),
            }
            entries.append(item)
            if item["tvg_id"]:
                ids.setdefault(item["tvg_id"], {"name": item["name"], "group": item["group"]})
                tvg_names.setdefault(item["tvg_id"], set()).add(item["name"])

    xml_ids: set[str] = set()
    total_counts: dict[str, int] = {}
    window_counts: dict[str, int] = {}
    blank_title: dict[str, int] = {}
    synthetic: dict[str, int] = {}
    dummy: dict[str, int] = {}
    programmes_by_id: dict[str, list[tuple]] = {}
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=3)
    quality_end = now + timedelta(hours=24)
    try:
        guide_days = max(1, min(3, int(get_setting("epg_window_days") or 4)))
    except (TypeError, ValueError):
        guide_days = 3
    guide_end = now + timedelta(days=guide_days)
    try:
        guide_cap = max(48, min(400, int(get_setting("guide_cache_max_programmes") or 320)))
    except (TypeError, ValueError):
        guide_cap = 320

    seen_programme = False
    well_ordered = True
    max_stop = ""
    current_channels: set[str] = set()
    parse_error = None
    if xml_path.exists():
        try:
            ctx = ET.iterparse(str(xml_path), events=("start", "end"))
            iterator = iter(ctx)
            _, root = next(iterator)
            for event, elem in iterator:
                tag = elem.tag.split("}", 1)[-1]
                if event != "end":
                    continue
                if tag == "channel":
                    if seen_programme:
                        well_ordered = False
                    cid = (elem.attrib.get("id") or "").strip()
                    if cid:
                        xml_ids.add(cid)
                    elem.clear()
                elif tag == "programme":
                    seen_programme = True
                    cid = (elem.attrib.get("channel") or "").strip()
                    if cid:
                        total_counts[cid] = total_counts.get(cid, 0) + 1
                    start_raw = elem.attrib.get("start", "")
                    stop_raw = elem.attrib.get("stop", "")
                    stop_key = stop_raw[:14]
                    if stop_key > max_stop:
                        max_stop = stop_key
                    start = _parse_xmltv_time(start_raw)
                    stop = _parse_xmltv_time(stop_raw)
                    if cid and start and stop:
                        if start <= now < stop:
                            current_channels.add(cid)
                        if cid in ids and stop >= window_start and start <= guide_end:
                            title_el = elem.find("title")
                            sub_el = elem.find("sub-title")
                            desc_el = elem.find("desc")
                            title = ((title_el.text or "") if title_el is not None else "").strip()
                            sub = ((sub_el.text or "") if sub_el is not None else "").strip()
                            desc = ((desc_el.text or "") if desc_el is not None else "").strip()
                            episode = ""
                            for epn in elem.findall("episode-num"):
                                if (epn.attrib.get("system") or "").lower() == "onscreen":
                                    episode = (epn.text or "").strip()
                                    if episode:
                                        break
                            bucket = programmes_by_id.setdefault(cid, [])
                            if len(bucket) < guide_cap:
                                bucket.append((start.isoformat(), stop.isoformat(), title, sub, episode, desc))
                            if start <= quality_end:
                                window_counts[cid] = window_counts.get(cid, 0) + 1
                                desc_lower = desc.lower()
                                if not title:
                                    blank_title[cid] = blank_title.get(cid, 0) + 1
                                if "schedule inferred from channel metadata" in desc_lower:
                                    synthetic[cid] = synthetic.get(cid, 0) + 1
                                if "no guide data" in desc_lower or title.lower().startswith("dummy."):
                                    dummy[cid] = dummy.get(cid, 0) + 1
                    elem.clear()
                    if len(root) > 256:
                        root.clear()
                elif tag not in {"title", "sub-title", "desc", "episode-num"}:
                    elem.clear()
        except (ET.ParseError, StopIteration) as exc:
            parse_error = str(exc)

    for programmes in programmes_by_id.values():
        programmes.sort(key=lambda p: p[0])
    return {
        "signature": signature, "built_at": time.time(), "entries": entries,
        "ids": ids, "tvg_names": tvg_names, "xml_ids": xml_ids,
        "total_counts": total_counts, "counts": window_counts,
        "blank_title": blank_title, "synthetic": synthetic, "dummy": dummy,
        "programmes_by_id": programmes_by_id, "well_ordered": well_ordered,
        "max_stop": max_stop, "current_channels": current_channels,
        "parse_error": parse_error,
    }


def _get_epg_snapshot() -> dict[str, Any]:
    global _epg_snapshot
    signature = _export_signature()
    if _epg_snapshot is not None and _epg_snapshot.get("signature") == signature:
        return _epg_snapshot
    with _epg_snapshot_lock:
        signature = _export_signature()
        if _epg_snapshot is None or _epg_snapshot.get("signature") != signature:
            _epg_snapshot = _build_epg_snapshot(signature)
        return _epg_snapshot


def _audit_epg_exports():
    """Inspect the exported M3U/XMLTV pair and report whether the guide is truly healthy."""
    report = {
        "status": "warning",
        "perfect": False,
        "summary": "EPG audit has not run yet.",
        "exported_channels": 0,
        "playlist_ids": 0,
        "xml_channels": 0,
        "xml_programmes": 0,
        "missing_playlist_tvg_ids": 0,
        "missing_in_xml": 0,
        "no_programmes": 0,
        "collisions": 0,
        "well_formed": False,
        "well_ordered": False,
        "sample_missing": [],
        "sample_missing_in_xml": [],
        "sample_no_programmes": [],
        "sample_collisions": [],
        "epg_newest_stop": None,
        "epg_hours_ahead": None,
        "channels_current": 0,
        "coverage_now_pct": 0.0,
    }

    m3u_path = EXPORT_DIR / "organized.m3u"
    xml_path = EXPORT_DIR / "organized.xml"
    if not m3u_path.exists() or not xml_path.exists():
        report.update(
            status="error",
            summary="Exports have not been generated yet. Run an export first.",
        )
        return report

    snapshot = _get_epg_snapshot()
    if snapshot.get("parse_error"):
        report.update(
            status="error",
            summary=f"XMLTV export is not valid XML: {snapshot['parse_error']}",
            well_formed=False,
            well_ordered=False,
        )
        return report

    entries = snapshot["entries"]
    playlist_ids = set(snapshot["ids"])
    xml_ids = snapshot["xml_ids"]
    programme_counts = snapshot["total_counts"]
    missing = [item for item in entries if not item["tvg_id"]]
    collisions = [
        {"tvg_id": tvg_id, "names": sorted(n for n in names if n)[:5]}
        for tvg_id, names in snapshot["tvg_names"].items() if len({n for n in names if n}) > 1
    ]
    missing_in_xml = sorted(playlist_ids - xml_ids)
    no_programmes = sorted(cid for cid in playlist_ids if cid in xml_ids and not programme_counts.get(cid))
    max_stop = snapshot["max_stop"]
    epg_hours_ahead = None
    if max_stop:
        try:
            newest = datetime.strptime(max_stop, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            epg_hours_ahead = round((newest - datetime.now(timezone.utc)).total_seconds() / 3600.0, 1)
        except ValueError:
            pass
    current = len(snapshot["current_channels"] & playlist_ids)
    report.update({
        "status": "ok", "well_formed": True, "well_ordered": snapshot["well_ordered"],
        "summary": "EPG export audit completed." if entries else "No exported playlist entries were found.",
        "exported_channels": len(entries), "playlist_ids": len(playlist_ids),
        "xml_channels": len(xml_ids), "xml_programmes": sum(programme_counts.values()),
        "missing_playlist_tvg_ids": len(missing), "sample_missing": missing[:10],
        "missing_in_xml": len(missing_in_xml), "no_programmes": len(no_programmes),
        "collisions": len(collisions), "sample_collisions": collisions[:10],
        "sample_missing_in_xml": [
            {"tvg_id": cid, **snapshot["ids"].get(cid, {"name": cid, "group": "Ungrouped"})}
            for cid in missing_in_xml[:10]
        ],
        "sample_no_programmes": [
            {"tvg_id": cid, **snapshot["ids"].get(cid, {"name": cid, "group": "Ungrouped"})}
            for cid in no_programmes[:10]
        ],
        "epg_newest_stop": max_stop or None, "epg_hours_ahead": epg_hours_ahead,
        "channels_current": current,
        "coverage_now_pct": round(100.0 * current / len(playlist_ids), 1) if playlist_ids else 0.0,
    })
    return report

    try:
        entries = []
        for line in m3u_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("#EXTINF"):
                continue
            attrs, display = parse_extinf_attrs(line)
            entries.append({
                "name": display or attrs.get("tvg-name") or "Unnamed",
                "group": attrs.get("group-title") or "Ungrouped",
                "tvg_id": (attrs.get("tvg-id") or "").strip(),
            })

        report["exported_channels"] = len(entries)
        tvg_lookup = {}
        tvg_names = {}
        missing = []
        playlist_ids = set()
        for item in entries:
            tvg_id = item["tvg_id"]
            if not tvg_id:
                missing.append({"name": item["name"], "group": item["group"]})
                continue
            playlist_ids.add(tvg_id)
            tvg_lookup.setdefault(tvg_id, item)
            tvg_names.setdefault(tvg_id, set()).add(item["name"])

        report["playlist_ids"] = len(playlist_ids)
        report["missing_playlist_tvg_ids"] = len(missing)
        report["sample_missing"] = missing[:10]

        collisions = []
        for tvg_id, names in tvg_names.items():
            clean_names = sorted(n for n in names if n)
            if len(clean_names) > 1:
                collisions.append({"tvg_id": tvg_id, "names": clean_names[:5]})
        report["collisions"] = len(collisions)
        report["sample_collisions"] = collisions[:10]

        xml_ids = set()
        programme_counts = {}
        seen_programme = False
        well_ordered = True
        # EPG freshness: every programme time is UTC (+0000), so the 14-char
        # YYYYMMDDHHMMSS prefix compares lexicographically. Track the newest
        # stop time and which channels have a programme airing right now.
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        max_stop = ""
        current_channels = set()
        for _event, elem in ET.iterparse(str(xml_path), events=("end",)):
            tag = elem.tag.split("}", 1)[-1]
            if tag == "channel":
                if seen_programme:
                    well_ordered = False
                cid = (elem.attrib.get("id") or "").strip()
                if cid:
                    xml_ids.add(cid)
            elif tag == "programme":
                seen_programme = True
                cid = (elem.attrib.get("channel") or "").strip()
                if cid:
                    programme_counts[cid] = programme_counts.get(cid, 0) + 1
                    st = (elem.attrib.get("start") or "")[:14]
                    sp = (elem.attrib.get("stop") or "")[:14]
                    if sp > max_stop:
                        max_stop = sp
                    if st and sp and st <= now_str < sp:
                        current_channels.add(cid)
            elem.clear()

        missing_in_xml = [cid for cid in sorted(playlist_ids) if cid not in xml_ids]
        no_programmes = [cid for cid in sorted(playlist_ids) if cid in xml_ids and programme_counts.get(cid, 0) == 0]

        report.update({
            "well_formed": True,
            "well_ordered": well_ordered,
            "xml_channels": len(xml_ids),
            "xml_programmes": sum(programme_counts.values()),
            "missing_in_xml": len(missing_in_xml),
            "no_programmes": len(no_programmes),
            "sample_missing_in_xml": [
                {
                    "tvg_id": cid,
                    "name": tvg_lookup.get(cid, {}).get("name", cid),
                    "group": tvg_lookup.get(cid, {}).get("group", "Ungrouped"),
                }
                for cid in missing_in_xml[:10]
            ],
            "sample_no_programmes": [
                {
                    "tvg_id": cid,
                    "name": tvg_lookup.get(cid, {}).get("name", cid),
                    "group": tvg_lookup.get(cid, {}).get("group", "Ungrouped"),
                }
                for cid in no_programmes[:10]
            ],
        })

        epg_hours_ahead = None
        if max_stop:
            try:
                newest = datetime.strptime(max_stop, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                epg_hours_ahead = round((newest - datetime.now(timezone.utc)).total_seconds() / 3600.0, 1)
            except Exception:
                pass
        cur = len(current_channels & playlist_ids)
        report.update({
            "epg_newest_stop": max_stop or None,
            "epg_hours_ahead": epg_hours_ahead,
            "channels_current": cur,
            "coverage_now_pct": round(100.0 * cur / len(playlist_ids), 1) if playlist_ids else 0.0,
        })

        if not entries:
            report["summary"] = "No exported playlist entries were found."
        else:
            report["summary"] = "EPG export audit completed."
        return report
    except ET.ParseError as exc:
        report.update(
            status="error",
            summary=f"XMLTV export is not valid XML: {exc}",
            well_formed=False,
            well_ordered=False,
        )
        return report
    except Exception as exc:
        report.update(status="error", summary=f"EPG audit failed: {exc}")
        return report


def _check_chno_drift(planned_export_complete: bool = False):
    """Detect channel-NUMBER drift in the exported playlist — the failure mode
    that put TiviMate/Televizo guides on the WRONG channel (Dispatcharr keys its
    Xtream EPG to the channel number, so a number that moves drags the guide with
    it). Compares each channel's tvg-chno to a stored baseline and flags any that
    moved, plus any duplicate numbers. Auto-rebaselines, so an intentional change
    alerts once and then becomes the new normal. Cheap: parses the ~3MB M3U only,
    never the EPG.

    Keyed by STREAM URL, not tvg-id: tvg-id is NOT unique (duplicate OTA mirrors
    like ShopLC on 45.11 & 69.11, and case-variant ids like shoplc.us/ShopLC.us,
    both resolve to the same id). Keying by tvg-id let two physical channels
    collide, so when their export order flipped the recorded number oscillated
    and produced phantom 'drift' every cycle. The stream URL is a stable, unique
    per-channel identity, so each real channel's number is tracked independently."""
    out = {"total": 0, "changed": [], "changed_count": 0,
           "duplicates": [], "duplicate_count": 0, "baseline_seeded": False,
           "group_order_mismatches": [], "group_order_mismatch_count": 0}
    m3u_path = EXPORT_DIR / "organized.m3u"
    if not m3u_path.exists():
        return out
    cur = {}            # url -> chno
    label = {}          # url -> "tvg-id (name)" for readable reporting
    chno_to_urls = {}
    group_numbers = {}
    pending = None      # the EXTINF attrs/name awaiting its URL line
    for line in m3u_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs, display = parse_extinf_attrs(line)
            pending = (attrs, display)
            continue
        if line.startswith("#"):
            continue
        # First non-comment line after an #EXTINF is that channel's stream URL.
        if pending is None:
            continue
        attrs, display = pending
        pending = None
        chno = (attrs.get("tvg-chno") or "").strip()
        if not chno:
            continue
        url = line
        cur[url] = chno
        tvg = (attrs.get("tvg-id") or "").strip()
        nm = (attrs.get("tvg-name") or display or "").strip()
        label[url] = f"{tvg or '?'} ({nm})" if nm else (tvg or url)
        chno_to_urls.setdefault(chno, []).append(url)
        group = (attrs.get("group-title") or "Ungrouped").strip()
        try:
            group_numbers.setdefault(group, []).append(int(chno))
        except ValueError:
            pass
    out["total"] = len(cur)
    out["duplicate_count"] = sum(1 for us in chno_to_urls.values() if len(us) > 1)
    out["duplicates"] = [{"chno": c, "tvg_ids": [label.get(u, u) for u in us[:6]]}
                         for c, us in chno_to_urls.items() if len(us) > 1][:20]
    bad_groups = []
    for group, numbers in group_numbers.items():
        inversions = sum(1 for a, b in zip(numbers, numbers[1:]) if a > b)
        if inversions:
            bad_groups.append({"group": group, "inversions": inversions, "channels": len(numbers)})
    out["group_order_mismatch_count"] = len(bad_groups)
    out["group_order_mismatches"] = sorted(
        bad_groups, key=lambda item: (-item["inversions"], item["group"].lower())
    )[:20]

    if get_setting("epg_chno_rebaseline_pending") == "1":
        # A watchdog can run in the debounce window between a deliberate
        # renumber and the replacement export landing. Do not consume the
        # marker against that old playlist: doing so makes the completed,
        # planned export look like mass drift on the following check.
        if not planned_export_complete:
            out["planned_rebaseline_waiting"] = True
            return out
        set_setting("epg_chno_baseline", json.dumps(cur))
        set_setting("epg_chno_rebaseline_pending", "0")
        out["baseline_seeded"] = True
        out["planned_rebaseline"] = True
        return out

    raw = get_setting("epg_chno_baseline")
    if not raw:
        set_setting("epg_chno_baseline", json.dumps(cur))
        out["baseline_seeded"] = True
        return out
    try:
        base = json.loads(raw)
    except Exception:
        base = {}
    changed = [{"tvg_id": label.get(u, u), "from": base[u], "to": cur[u]}
               for u in cur if u in base and base[u] != cur[u]]
    out["changed_count"] = len(changed)
    out["changed"] = changed[:30]
    set_setting("epg_chno_baseline", json.dumps(cur))   # rebaseline: report once
    return out


def _run_epg_watchdog_check():
    """Audit EPG export integrity and auto-heal with a re-export when needed.
    Also runs the drift signals (channel-number drift + EPG staleness) that a
    re-export can't fix, and pushes a separate ntfy alert when they appear."""
    global _epg_watchdog_status

    audit = _audit_epg_exports()
    issues = []
    # Distinguish issues a re-export can actually fix from chronic upstream
    # data gaps it can't.  Channels without tvg-id in the source feed will
    # never gain a tvg-id by re-exporting — running write_exports for that
    # only burns CPU rebuilding 120MB XML to identical result.  Same for
    # "no_programmes" against channels whose upstream EPG genuinely has
    # nothing for them.  Only heal on transient conditions.
    fixable_issues = []
    chronic_issues = []
    if audit.get("status") == "error":
        fixable_issues.append(audit.get("summary") or "EPG audit error")
    if not audit.get("well_formed", True):
        fixable_issues.append("XMLTV is not well-formed")
    if audit.get("missing_in_xml"):
        # tvg-id is set on the channel but isn't in the XMLTV — a re-export
        # *can* fix this if the upstream EPG actually has data for it.
        fixable_issues.append(f"{audit['missing_in_xml']} playlist tvg-id(s) missing in XMLTV")
    if audit.get("missing_playlist_tvg_ids"):
        chronic_issues.append(f"{audit['missing_playlist_tvg_ids']} exported channel(s) missing tvg-id")
    if audit.get("no_programmes"):
        chronic_issues.append(f"{audit['no_programmes']} channel(s) have no programmes")
    issues = fixable_issues + chronic_issues

    now_iso = datetime.now(timezone.utc).isoformat()
    status = {
        "last_check": now_iso,
        "last_ok": now_iso if not issues else _epg_watchdog_status.get("last_ok"),
        "status": "ok" if not issues else "degraded",
        "issues": issues,
        "self_heal_attempted": False,
        "self_heal_success": None,
        "audit": audit,
    }

    if fixable_issues:
        status["self_heal_attempted"] = True
        try:
            write_exports()
            audit_after = _audit_epg_exports()
            healed = (
                audit_after.get("status") != "error"
                and not audit_after.get("missing_playlist_tvg_ids")
                and not audit_after.get("missing_in_xml")
                and not audit_after.get("no_programmes")
                and audit_after.get("well_formed", True)
            )
            status["self_heal_success"] = healed
            status["audit"] = audit_after
            if healed:
                status["status"] = "ok"
                status["issues"] = []
                status["last_ok"] = now_iso
            _refresh_guide_cache()
        except Exception as exc:
            status["self_heal_success"] = False
            status["issues"] = issues + [f"self-heal failed: {exc}"]

    # Notify on a degraded guide that couldn't self-heal — the case worth a
    # push (transient blips that self-heal stay quiet). Only fire on a
    # transition into the bad state to avoid repeat spam every cadence.
    prev_status = getattr(_run_epg_watchdog_check, "_last_notified_status", "ok")
    if status["status"] != "ok" and prev_status == "ok":
        _notify("M3U Boss: EPG guide degraded",
                "; ".join(status.get("issues") or ["unknown issue"]),
                priority="high", tags="warning,tv")
    _run_epg_watchdog_check._last_notified_status = status["status"]

    # --- Drift signals: channel-number drift + EPG staleness ---
    # These are NOT fixable by a re-export, so they're tracked and alerted on
    # separately from the integrity self-heal flow above.
    try:
        drift = _check_chno_drift()
    except Exception as exc:
        _record_backend_error("epg_drift.chno", exc)
        drift = {}
    fresh = {
        "newest_stop": audit.get("epg_newest_stop"),
        "hours_ahead": audit.get("epg_hours_ahead"),
        "coverage_now_pct": audit.get("coverage_now_pct"),
        "channels_current": audit.get("channels_current"),
    }
    ha = fresh.get("hours_ahead")
    status["epg_fresh"] = (ha is None) or (ha >= _EPG_STALE_HOURS)
    status["drift"] = drift
    status["epg_freshness"] = fresh

    # drift_alerts holds the human-readable text (embeds live values like the
    # exact coverage %, so it changes every check); drift_keys holds stable
    # category ids used to decide whether to notify. Comparing the literal
    # text meant any wobble in a live value (coverage 83.4% -> 82.1%, still
    # both below the 85% line) looked like a "new" alert and re-fired on
    # nearly every watchdog pass. Comparing categories instead means it only
    # notifies when the SET of active problems changes, not when a number
    # sitting past a threshold moves around.
    drift_alerts = []
    drift_keys = []
    if drift.get("changed_count"):
        eg = ", ".join(f"{c['tvg_id']} {c['from']}→{c['to']}"
                       for c in drift.get("changed", [])[:3])
        drift_alerts.append(f"{drift['changed_count']} channel number(s) drifted ({eg})")
        drift_keys.append("chno_drift")
    if drift.get("duplicate_count"):
        drift_alerts.append(f"{drift['duplicate_count']} duplicate channel number(s)")
        drift_keys.append("chno_duplicate")
    if drift.get("group_order_mismatch_count"):
        names = ", ".join(
            item["group"] for item in drift.get("group_order_mismatches", [])[:3]
        )
        drift_alerts.append(
            f"{drift['group_order_mismatch_count']} group(s) have display/number order mismatch ({names})"
        )
        drift_keys.append("chno_group_order")
    if ha is not None and ha < _EPG_STALE_HOURS:
        drift_alerts.append(f"EPG is stale — guide only reaches {ha}h ahead")
        drift_keys.append("epg_stale")
    cov = fresh.get("coverage_now_pct")
    if cov is not None and cov < 85.0:
        drift_alerts.append(f"EPG coverage dropped to {cov:.1f}% (below 85%)")
        drift_keys.append("epg_coverage_low")
    status["drift_alerts"] = drift_alerts

    # Notify only when the set of active categories changes (transition), to
    # avoid repeat spam. Store the last category set in SQLite too, so a
    # container restart does not resend an unchanged standing alert on the
    # first watchdog pass.
    prev_keys = getattr(_run_epg_watchdog_check, "_last_drift_keys", None)
    if prev_keys is None:
        prev_keys = _load_json_setting("epg_last_drift_keys", [])
    if drift_alerts and drift_keys != prev_keys:
        _notify("M3U Boss: EPG drift detected", "; ".join(drift_alerts),
                priority="high", tags="rotating_light,tv")
    _run_epg_watchdog_check._last_drift_keys = drift_keys
    if drift_keys != prev_keys:
        set_setting("epg_last_drift_keys", json.dumps(drift_keys, separators=(",", ":")))

    _epg_watchdog_status = status
    return status


def _build_export_programme_index(window_back_hours: int = 3, window_forward_hours: int = 24):
    """Build an index of exported playlist ids and programme quality in the active guide window."""
    if window_back_hours == 3 and window_forward_hours == 24:
        snapshot = _get_epg_snapshot()
        return {
            "ids": snapshot["ids"],
            "counts": snapshot["counts"],
            "blank_title": snapshot["blank_title"],
            "synthetic": snapshot["synthetic"],
            "dummy": snapshot["dummy"],
        }
    m3u_path = EXPORT_DIR / "organized.m3u"
    xml_path = EXPORT_DIR / "organized.xml"
    ids = {}
    if m3u_path.exists():
        for line in m3u_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("#EXTINF"):
                continue
            attrs, display = parse_extinf_attrs(line)
            tvg_id = (attrs.get("tvg-id") or "").strip()
            if not tvg_id:
                continue
            ids[tvg_id] = {
                "name": display or attrs.get("tvg-name") or "Unnamed",
                "group": attrs.get("group-title") or "Ungrouped",
            }

    counts = {}
    blank_title = {}
    synthetic = {}
    dummy = {}
    if not xml_path.exists():
        return {
            "ids": ids,
            "counts": counts,
            "blank_title": blank_title,
            "synthetic": synthetic,
            "dummy": dummy,
        }

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_back_hours)
    window_end = now + timedelta(hours=window_forward_hours)
    fmt = "%Y%m%d%H%M%S %z"
    fmt_alt = "%Y%m%d%H%M%S"

    try:
        for _event, elem in ET.iterparse(str(xml_path), events=("end",)):
            tag = elem.tag.split("}", 1)[-1]
            if tag != "programme":
                elem.clear()
                continue
            cid = (elem.attrib.get("channel") or "").strip()
            if not cid or cid not in ids:
                elem.clear()
                continue
            start_str = elem.attrib.get("start", "")
            stop_str = elem.attrib.get("stop", "")
            try:
                start = datetime.strptime(start_str, fmt)
            except ValueError:
                try:
                    start = datetime.strptime(start_str[:14], fmt_alt).replace(tzinfo=timezone.utc)
                except ValueError:
                    elem.clear()
                    continue
            try:
                stop = datetime.strptime(stop_str, fmt)
            except ValueError:
                try:
                    stop = datetime.strptime(stop_str[:14], fmt_alt).replace(tzinfo=timezone.utc)
                except ValueError:
                    elem.clear()
                    continue
            if stop < window_start or start > window_end:
                elem.clear()
                continue

            counts[cid] = counts.get(cid, 0) + 1
            title_el = elem.find("title")
            desc_el = elem.find("desc")
            title = ((title_el.text or "") if title_el is not None else "").strip()
            desc = ((desc_el.text or "") if desc_el is not None else "").strip().lower()
            if not title:
                blank_title[cid] = blank_title.get(cid, 0) + 1
            if "schedule inferred from channel metadata" in desc:
                synthetic[cid] = synthetic.get(cid, 0) + 1
            if "no guide data" in desc or title.lower().startswith("dummy."):
                dummy[cid] = dummy.get(cid, 0) + 1
            elem.clear()
    except ET.ParseError:
        return {
            "ids": ids,
            "counts": {},
            "blank_title": {},
            "synthetic": {},
            "dummy": {},
        }

    return {
        "ids": ids,
        "counts": counts,
        "blank_title": blank_title,
        "synthetic": synthetic,
        "dummy": dummy,
    }


def _suspicious_tvg_id(tvg_id: str) -> bool:
    tid = (tvg_id or "").strip().lower()
    if not tid:
        return True
    if tid.startswith("dummy-") or tid.startswith("dummy."):
        return True
    bad_exact = {
        "s+.pt", "eentertainment.us", "tv.us", "pop.us", "talktv.uk",
        "e!entertainmenttelevisioncanada.ca", "own.us",
    }
    if tid in bad_exact:
        return True
    if tid.isdigit():
        return True
    return False


# ── Sports city → team database ───────────────────────────────────
SPORTS_CITIES: dict[str, dict] = {
    "Arizona": {"teams": ["Cardinals", "Diamondbacks", "Coyotes", "Suns", "Mercury", "Arizona State Sun Devils"]},
    "Atlanta": {"teams": ["Falcons", "Braves", "Hawks", "Atlanta United", "Dream", "Georgia Bulldogs", "Georgia Tech Yellow Jackets"]},
    "Baltimore": {"teams": ["Ravens", "Orioles", "Maryland Terrapins"]},
    "Boston": {"teams": ["Patriots", "Red Sox", "Celtics", "Bruins", "Revolution", "Boston College Eagles"]},
    "Buffalo": {"teams": ["Bills", "Sabres"]},
    "Carolina": {"teams": ["Panthers", "Hurricanes", "Charlotte Hornets", "Charlotte FC", "Clemson Tigers", "Duke Blue Devils", "NC State Wolfpack", "North Carolina Tar Heels", "Wake Forest Demon Deacons"]},
    "Chicago": {"teams": ["Bears", "Cubs", "White Sox", "Bulls", "Blackhawks", "Fire", "Sky", "Northwestern Wildcats", "Illinois Fighting Illini"]},
    "Cincinnati": {"teams": ["Bengals", "Reds", "FC Cincinnati", "Cincinnati Bearcats"]},
    "Cleveland": {"teams": ["Browns", "Guardians", "Cavaliers", "Ohio State Buckeyes"]},
    "Colorado": {"teams": ["Broncos", "Rockies", "Nuggets", "Avalanche", "Rapids", "Colorado Buffaloes"]},
    "Columbus": {"teams": ["Columbus Crew", "Ohio State Buckeyes"]},
    "Dallas": {"teams": ["Cowboys", "Rangers", "Mavericks", "Stars", "FC Dallas", "Wings", "TCU Horned Frogs", "SMU Mustangs", "Texas Longhorns", "Texas A&M Aggies", "Baylor Bears"]},
    "Denver": {"teams": ["Broncos", "Rockies", "Nuggets", "Avalanche", "Rapids", "Colorado Buffaloes"]},
    "Detroit": {"teams": ["Lions", "Tigers", "Pistons", "Red Wings", "Michigan Wolverines", "Michigan State Spartans"]},
    "Green Bay": {"teams": ["Packers", "Wisconsin Badgers"]},
    "Houston": {"teams": ["Texans", "Astros", "Rockets", "Dynamo", "Dash", "Houston Cougars"]},
    "Indianapolis": {"teams": ["Colts", "Pacers", "Fever", "Indiana Hoosiers", "Purdue Boilermakers", "Notre Dame Fighting Irish"]},
    "Jacksonville": {"teams": ["Jaguars", "Florida Gators", "Florida State Seminoles"]},
    "Kansas City": {"teams": ["Chiefs", "Royals", "Sporting KC", "Current", "Kansas Jayhawks", "Kansas State Wildcats"]},
    "Las Vegas": {"teams": ["Raiders", "Golden Knights", "Aces", "UNLV Rebels"]},
    "Los Angeles": {"teams": ["Rams", "Chargers", "Dodgers", "Angels", "Lakers", "Clippers", "Kings", "Ducks", "Galaxy", "LAFC", "Sparks", "UCLA Bruins", "USC Trojans"]},
    "Memphis": {"teams": ["Grizzlies", "Memphis Tigers"]},
    "Miami": {"teams": ["Dolphins", "Marlins", "Heat", "Panthers", "Inter Miami", "Miami Hurricanes", "Florida Gators", "Florida State Seminoles"]},
    "Milwaukee": {"teams": ["Brewers", "Bucks", "Wisconsin Badgers", "Marquette Golden Eagles"]},
    "Minnesota": {"teams": ["Vikings", "Twins", "Timberwolves", "Wild", "Minnesota United", "Lynx", "Minnesota Golden Gophers"]},
    "Nashville": {"teams": ["Titans", "Predators", "Nashville SC", "Vanderbilt Commodores", "Tennessee Volunteers"]},
    "New Orleans": {"teams": ["Saints", "Pelicans", "LSU Tigers", "Tulane Green Wave"]},
    "New York": {"teams": ["Giants", "Jets", "Yankees", "Mets", "Knicks", "Nets", "Rangers", "Islanders", "Devils", "NYCFC", "Red Bulls", "Liberty", "St. John's Red Storm", "Syracuse Orange", "Rutgers Scarlet Knights"]},
    "Oklahoma City": {"teams": ["Thunder", "Oklahoma Sooners", "Oklahoma State Cowboys"]},
    "Orlando": {"teams": ["Magic", "Orlando City", "Pride", "UCF Knights"]},
    "Philadelphia": {"teams": ["Eagles", "Phillies", "76ers", "Sixers", "Flyers", "Union", "Penn State Nittany Lions", "Temple Owls", "Villanova Wildcats"]},
    "Pittsburgh": {"teams": ["Steelers", "Pirates", "Penguins", "Pitt Panthers", "Pittsburgh Panthers", "West Virginia Mountaineers", "Penn State Nittany Lions"]},
    "Portland": {"teams": ["Trail Blazers", "Blazers", "Timbers", "Thorns", "Oregon Ducks", "Oregon State Beavers"]},
    "Sacramento": {"teams": ["Kings", "Republic", "California Golden Bears"]},
    "Salt Lake City": {"teams": ["Jazz", "Real Salt Lake", "Utah Utes", "BYU Cougars"]},
    "San Antonio": {"teams": ["Spurs", "Texas Longhorns", "Texas A&M Aggies"]},
    "San Diego": {"teams": ["Padres", "Wave", "San Diego State Aztecs"]},
    "San Francisco": {"teams": ["49ers", "Niners", "Giants", "Warriors", "Stanford Cardinal", "California Golden Bears"]},
    "Seattle": {"teams": ["Seahawks", "Mariners", "Kraken", "Sounders", "Storm", "Reign", "Washington Huskies"]},
    "St. Louis": {"teams": ["Cardinals", "Blues", "City SC", "Missouri Tigers", "Illinois Fighting Illini"]},
    "Tampa Bay": {"teams": ["Buccaneers", "Bucs", "Rays", "Lightning", "USF Bulls", "UCF Knights", "Florida Gators", "Florida State Seminoles"]},
    "Toronto": {"teams": ["Raptors", "Blue Jays", "Maple Leafs", "Toronto FC"]},
    "Washington DC": {"teams": ["Commanders", "Nationals", "Wizards", "Capitals", "DC United", "Mystics", "Georgetown Hoyas", "Maryland Terrapins", "Virginia Cavaliers", "Virginia Tech Hokies"]},
}


# ═══════════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════════

APP_VERSION = "0.2.0-alpha.1"
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    threading.Thread(target=_startup_background_init, daemon=True).start()
    threading.Thread(target=_guide_bg_loop, daemon=True).start()
    threading.Thread(target=_source_refresh_loop, daemon=True).start()
    yield


app = FastAPI(title="M3U Boss", version=APP_VERSION, lifespan=lifespan)
app.include_router(create_release_features_router(schedule_export, EXPORT_DIR))

_ADMIN_USERNAME = os.environ.get("M3U_BOSS_ADMIN_USERNAME", "admin")
_ADMIN_PASSWORD = os.environ.get("M3U_BOSS_ADMIN_PASSWORD", "")
_FEED_TOKEN = os.environ.get("M3U_BOSS_FEED_TOKEN", "")
_ALLOW_INSECURE = os.environ.get("M3U_BOSS_ALLOW_INSECURE", "0") == "1"
_UNAUTHENTICATED_PATHS = {
    "/healthz", "/player_api.php", "/xmltv.php", "/teamarr.xml",
}
_UNAUTHENTICATED_PREFIXES = ("/static/", "/live/", "/logos/")
_FEED_PATHS = {"/api/export/m3u", "/api/export/epg", "/m3u", "/epg", "/epg.xml"}
_auth_failure_lock = threading.Lock()
_auth_failures: dict[str, deque[float]] = {}


@app.middleware("http")
async def require_admin_auth(request: Request, call_next):
    """Protect the management surface with HTTP Basic authentication.

    IPTV feed and Teamarr endpoints use their own credentials and remain
    reachable by clients. An explicit insecure opt-out exists for trusted
    development networks, but the packaged deployment requires a password.
    """
    path = request.url.path
    if (path == "/favicon.ico" or path in _UNAUTHENTICATED_PATHS
            or any(path.startswith(p) for p in _UNAUTHENTICATED_PREFIXES)):
        return await call_next(request)
    if path in _FEED_PATHS and _FEED_TOKEN and secrets.compare_digest(
            request.query_params.get("token", ""), _FEED_TOKEN):
        return await call_next(request)
    if _ALLOW_INSECURE:
        return await call_next(request)
    if not _ADMIN_PASSWORD:
        return Response(
            "M3U Boss is not configured. Set M3U_BOSS_ADMIN_PASSWORD.",
            status_code=503,
            media_type="text/plain",
        )
    auth = request.headers.get("authorization", "")
    supplied_user = supplied_password = ""
    if auth.lower().startswith("basic "):
        try:
            supplied_user, supplied_password = base64.b64decode(
                auth.split(" ", 1)[1], validate=True
            ).decode("utf-8").split(":", 1)
        except (ValueError, UnicodeDecodeError):
            pass
    valid = (secrets.compare_digest(supplied_user, _ADMIN_USERNAME)
             and secrets.compare_digest(supplied_password, _ADMIN_PASSWORD))
    if not valid:
        client = request.client.host if request.client else "unknown"
        now = time.time()
        with _auth_failure_lock:
            attempts = _auth_failures.setdefault(client, deque())
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
            attempts.append(now)
            limited = len(attempts) > 20
        if limited:
            return Response("Too many authentication attempts", status_code=429,
                            headers={"Retry-After": "60"}, media_type="text/plain")
        return Response(
            "Authentication required", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="M3U Boss"'},
            media_type="text/plain",
        )
    if request.client:
        with _auth_failure_lock:
            _auth_failures.pop(request.client.host, None)
    origin = request.headers.get("origin")
    if origin and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        same_origin = str(request.base_url).rstrip("/")
        public_origin = (os.environ.get("M3U_BOSS_PUBLIC_URL") or "").rstrip("/")
        configured = {item.strip().rstrip("/") for item in
                      os.environ.get("M3U_BOSS_CORS_ORIGINS", "").split(",") if item.strip()}
        if origin.rstrip("/") not in ({same_origin, public_origin} | configured):
            return Response("Untrusted request origin", status_code=403,
                            media_type="text/plain")
    # High-impact operations receive a compact, credential-free lineup undo
    # point. Repeated calls of the same type within a minute reuse a snapshot,
    # avoiding excessive database growth during batch workflows.
    snapshot = None
    method_path = f"{request.method} {path}"
    high_impact = (
        (request.method == "POST" and path in {
            "/api/apply-rules", "/api/sources/reset", "/api/groups/reorder",
            "/api/groups/renumber-all", "/api/restore", "/api/import/xc",
            "/api/import/m3u",
        })
        or (request.method == "POST" and re.match(r"^/api/sources/\d+/refresh$", path))
        or (request.method == "DELETE" and re.match(r"^/api/sources/\d+$", path))
        or (request.method in {"POST", "DELETE"} and any(part in path for part in (
            "/bulk-", "/reorder-channels", "/renumber-block", "/sort-alpha",
            "/sort-by-source", "/sort-channels-by-priority",
        )))
        or (request.method == "DELETE" and path.startswith("/api/groups/"))
    )
    if high_impact:
        try:
            snapshot = lineup_history.create_snapshot(
                f"Before {method_path}", "automatic", dedupe_seconds=60
            )
        except Exception as exc:
            _record_backend_error("history.snapshot", exc)
    response = await call_next(request)
    if high_impact and snapshot:
        try:
            lineup_history.record_action(
                method_path, "Automatic safety point",
                snapshot.get("id"), "completed" if response.status_code < 400 else "failed",
            )
        except Exception as exc:
            _record_backend_error("history.action", exc)
    return response


# ── Nightly DB backup ──────────────────────────────────────────────────────
# A botched import or rule change can wipe manual channel curation (group
# assignments aren't easily reconstructed), so keep a rolling set of nightly
# snapshots of sources.db. Taken with SQLite's online backup API, which is
# consistent even while the app is actively writing (WAL-safe). Snapshots live
# in data/backups/sources.db.<timestamp>; the newest BACKUP_KEEP are retained.
_BACKUP_DB_FILE = Path(__file__).resolve().parent.parent / "data" / "sources.db"
_BACKUP_DIR = _BACKUP_DB_FILE.parent / "backups"
_BACKUP_KEEP = 14          # ~two weeks of nightly snapshots
_BACKUP_HOUR = 3           # 03:30 local — quiet hour
_BACKUP_MIN = 30
_backup_log = logging.getLogger("uvicorn.error")

def _backup_db_once():
    """Write one consistent, gzip-compressed snapshot of sources.db and prune old ones.

    The snapshot is taken with SQLite's online backup API into a temp .db, then
    gzip-compressed (~10x: 28 MB -> ~3 MB) so two weeks of nightly snapshots cost
    tens of MB instead of ~400 MB. Restore: `gunzip -c <snap>.gz > sources.db`.
    """
    import shutil as _shutil
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _BACKUP_DIR / f"sources.db.{stamp}.gz"
    tmp = _BACKUP_DIR / f".sources.db.{stamp}.tmp"   # leading dot: excluded from glob
    src = sqlite3.connect(str(_BACKUP_DB_FILE))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            with dst:
                src.backup(dst)          # online, point-in-time consistent
        finally:
            dst.close()
    finally:
        src.close()
    try:
        with open(tmp, "rb") as fin, gzip.open(dest, "wb", compresslevel=6) as fout:
            _shutil.copyfileobj(fin, fout, length=1 << 20)
    finally:
        try: tmp.unlink()
        except OSError: pass
    # Prune oldest; glob still matches legacy uncompressed snapshots during transition.
    snaps = sorted(_BACKUP_DIR.glob("sources.db.*"))
    for old in snaps[:-_BACKUP_KEEP]:
        try: old.unlink()
        except OSError: pass
    _backup_log.info("sources.db backup written: %s (%d retained)",
                     dest.name, min(len(snaps), _BACKUP_KEEP))
    return dest

def _backup_loop():
    """Snapshot on startup (if none is fresh) then nightly at _BACKUP_HOUR:MIN."""
    try:
        snaps = sorted(_BACKUP_DIR.glob("sources.db.*")) if _BACKUP_DIR.exists() else []
        fresh = snaps and (time.time() - snaps[-1].stat().st_mtime) < 12 * 3600
        if not fresh:
            _backup_db_once()
    except Exception:
        _backup_log.exception("startup db backup failed")
    while True:
        now = datetime.now()
        target = now.replace(hour=_BACKUP_HOUR, minute=_BACKUP_MIN, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        time.sleep(max(60, (target - now).total_seconds()))
        try:
            _backup_db_once()
        except Exception:
            _backup_log.exception("nightly db backup failed")


def _startup_background_init():
    """Run startup maintenance serially after the app begins serving.

    The SQLite online backup and export both read the full database. Starting
    them concurrently on Unraid's FUSE-backed appdata path can produce a
    transient ``disk I/O error``, so the backup loop begins only after initial
    grouping/export/watchdog work finishes.
    """
    has_channels = False
    try:
        active_ids = get_active_source_ids()
        has_channels = bool(get_all_channels_raw(source_id=active_ids))
        if not has_channels:
            _prewarm_dashboard_cache()
            threading.Thread(target=_backup_loop, name="db-backup", daemon=True).start()
            return
        assigned = _assign_new_channels()
        m3u_path = EXPORT_DIR / "organized.m3u"
        epg_path = EXPORT_DIR / "organized.xml"
        exports_missing = not (
            m3u_path.exists() and m3u_path.stat().st_size > 0
            and epg_path.exists() and epg_path.stat().st_size > 0
        )
        export_stale = False
        if not exports_missing:
            startup_groups = sorted(
                list_groups(source_id=active_ids), key=lambda g: g.get("sort_order", 0)
            )
            expected_count = len(_collect_selected_channels(startup_groups, active_ids))
            with open(m3u_path, "r", encoding="utf-8", errors="ignore") as handle:
                exported_count = sum(1 for line in handle if line.startswith("#EXTINF"))
            export_stale = expected_count != exported_count
        if assigned or exports_missing or export_stale:
            write_exports()
    except Exception as exc:
        _record_backend_error("startup.export", exc)
    if has_channels:
        _maybe_run_epg_watchdog(force=True)
    _prewarm_dashboard_cache()
    threading.Thread(target=_backup_loop, name="db-backup", daemon=True).start()

@app.post("/api/backup/now")
def api_backup_now():
    """Take an on-demand sources.db snapshot right now."""
    dest = _backup_db_once()
    return {"ok": True, "file": dest.name}

@app.get("/api/backup/list")
def api_backup_list():
    """List retained nightly/manual snapshots, newest first."""
    snaps = sorted(_BACKUP_DIR.glob("sources.db.*"), reverse=True) if _BACKUP_DIR.exists() else []
    return {"backups": [{"file": s.name, "bytes": s.stat().st_size,
                         "mtime": int(s.stat().st_mtime)} for s in snaps]}


_cors_origins = [origin.strip() for origin in
                 os.environ.get("M3U_BOSS_CORS_ORIGINS", "").split(",")
                 if origin.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware, allow_origins=_cors_origins, allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.add_middleware(GZipMiddleware, minimum_size=1000)

class CacheStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
        return response


app.mount("/static", CacheStaticFiles(directory=BASE_DIR / "app" / "static"), name="static")

# Match-logo composites (combined "Team A vs Team B" badges) served at /logos.
# The Go edge reverse-proxies non-(m3u/epg) paths to this backend, so TiviMate
# fetches these via the edge (:43817) with no edge changes. Dir lives under
# data/ (the /mnt/cache bind) so composites persist across rebuilds.
_MATCH_LOGO_DIR = BASE_DIR / "data" / "match_logos"
_MATCH_LOGO_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/logos", StaticFiles(directory=_MATCH_LOGO_DIR), name="logos")

import traceback as _tb
import logging as _logging
_log_main = _logging.getLogger("m3u_boss")

@app.exception_handler(Exception)
async def _handle_unhandled_exception(request: Request, exc: Exception):
    tb = _tb.format_exc()
    _log_main.error("Unhandled exception on %s %s:\n%s", request.method, request.url.path,
                    _redact_text(tb))
    if not DEBUG_TRACEBACKS:
        _record_backend_error(f"unhandled.{request.method}.{request.url.path}", exc)
        return Response(
            content=json.dumps({"detail": "Internal server error"}),
            status_code=500,
            media_type="application/json",
        )
    return Response(
        content=json.dumps({"detail": str(exc), "traceback": tb}),
        status_code=500,
        media_type="application/json",
    )

@app.get("/")
def index():
    index_path = BASE_DIR / "app" / "static" / "index.html"
    html = index_path.read_text(encoding="utf-8")

    # Auto-bust CSS/JS caches based on file mtime so updates appear reliably.
    styles_v = int((BASE_DIR / "app" / "static" / "styles.css").stat().st_mtime)
    app_v = int((BASE_DIR / "app" / "static" / "app.js").stat().st_mtime)
    html = re.sub(r"/static/styles\.css\?v=\d+", f"/static/styles.css?v={styles_v}", html)
    html = re.sub(r"/static/app\.js\?v=\d+", f"/static/app.js?v={app_v}", html)

    return HTMLResponse(
        content=html,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/healthz")
def healthz():
    """Cheap readiness probe with no private configuration in its payload."""
    return {"status": "ok", "version": APP_VERSION}


# ── Dashboard ─────────────────────────────────────────────────────

@app.get("/api/dashboard")
def api_dashboard():
    sources = list_sources()
    # Keep dashboard responses snappy; refresh XC account metadata in background.
    _refresh_xc_account_status_async(sources)

    def _build():
        fresh_sources = list_sources()
        actives = [s for s in fresh_sources if s.get("is_active")]
        asids = [s["id"] for s in actives]
        stats = channel_stats(source_id=asids or None)
        settings = get_all_settings()
        interval = _get_refresh_interval()
        elapsed = time.time() - _last_source_refresh if _last_source_refresh else 0
        next_in = max(0, interval - elapsed)
        public_sources = [_public_source(s) for s in fresh_sources]
        public_actives = [s for s in public_sources if s.get("is_active")]
        return {
            "stats": stats,
            "sources": public_sources,
            "active_sources": public_actives,
            "active_source": public_actives[0] if public_actives else None,  # backward compat
            "teamarr_enabled": settings.get("teamarr_enabled") == "1",
            "teamarr_username": settings.get("teamarr_username", ""),
            "health": {
                "last_sync": datetime.fromtimestamp(_last_source_refresh, tz=timezone.utc).isoformat() if _last_source_refresh else None,
                "next_sync_seconds": int(next_in),
                "export_status": _export_status.get("status", "idle"),
                "export_time": _export_status.get("time"),
                "export_channels": _export_status.get("channels", 0),
                "export_error": _export_status.get("error"),
            },
        }

    return _cached_response("dashboard", ttl_sec=8.0, build_fn=_build)


@app.get("/api/bootstrap")
def api_bootstrap():
    """Small first-paint payload for nav badges and dashboard shell."""
    dashboard = api_dashboard()
    history = api_export_history(12)
    errors = api_system_errors(12)
    return {
        "dashboard": dashboard,
        "history": history,
        "errors": errors,
        "source_count": len(dashboard.get("sources") or []),
        "channel_count": (dashboard.get("stats") or {}).get("total_channels", 0),
    }


# ── Sources ───────────────────────────────────────────────────────

@app.get("/api/sources")
def api_sources(refresh_account: bool = Query(False)):
    sources = list_sources()
    if refresh_account:
        _refresh_xc_account_status(sources)
        sources = list_sources()
    return [_public_source(s) for s in sources]


@app.get("/api/channel/alternatives")
def api_channel_alternatives(url: str, refresh: bool = Query(False)):
    """Given the URL of a currently-playing channel, return alternative URLs
    using each of the OTHER enabled XC sources (which share the same channel
    lineup).  Ranked by available capacity (max_connections - active_connections).

    Used by simpletv (and any other consumer) to fail over when a source's
    credentials hit their connection limit.
    """
    # XC URLs are `{server}/live/{user}/{pass}/{stream_id}.{ext}` or
    # `{server}/movie/...` / `{server}/series/...`.  Parse stream_id +
    # category (live / movie / series) from the path.
    import re as _re
    m = _re.match(r'^(https?://[^/]+)/(live|movie|series)/([^/]+)/([^/]+)/(\d+)\.([a-z0-9]+)$', url)
    if not m:
        raise HTTPException(400, "URL doesn't match Xtream pattern; can't compute alternatives")
    base_path, category, cur_user, cur_pass, stream_id, ext = m.groups()
    cur_server = base_path

    sources = get_active_sources()
    if refresh:
        _refresh_xc_account_status(sources)
        sources = get_active_sources()

    alternatives = []
    for s in sources:
        if s.get("type") != "xc": continue
        u = (s.get("xc_username") or '').strip()
        p = (s.get("xc_password") or '').strip()
        srv = (s.get("xc_server") or '').rstrip('/')
        if not (u and p and srv): continue
        # Skip the current credentials.
        if u == cur_user and p == cur_pass and srv == cur_server: continue
        max_c = int(s.get("max_connections") or 0)
        act_c = int(s.get("active_connections") or 0)
        free = max(0, max_c - act_c) if max_c else 1  # unknown → assume 1 free
        alt_url = f"{srv}/{category}/{u}/{p}/{stream_id}.{ext}"
        alternatives.append({
            "url": alt_url,
            "source_id": s.get("id"),
            "source_name": s.get("name") or '',
            "username": u,
            "server": srv,
            "max_connections": max_c,
            "active_connections": act_c,
            "free_slots": free,
            "expires_at": s.get("expires_at"),
            "is_expired": s.get("is_expired"),
        })

    # Rank by free capacity first, then by most recently updated source.
    alternatives.sort(key=lambda a: (-a["free_slots"], a["is_expired"] or False))
    return {
        "current": {
            "server": cur_server, "username": cur_user,
            "category": category, "stream_id": stream_id, "ext": ext,
        },
        "alternatives": alternatives,
        "count": len(alternatives),
    }


@app.get("/api/credentials")
def api_credentials():
    """Return every enabled XC source's credential set.  Used by downstream
    apps (simpletv) that want to manage failover themselves.  Treat the
    response as sensitive — passwords are in it."""
    sources = get_active_sources()
    creds = []
    for s in sources:
        if s.get("type") != "xc": continue
        u = (s.get("xc_username") or '').strip()
        p = (s.get("xc_password") or '').strip()
        srv = (s.get("xc_server") or '').rstrip('/')
        if not (u and p and srv): continue
        max_c = int(s.get("max_connections") or 0)
        act_c = int(s.get("active_connections") or 0)
        creds.append({
            "id": s.get("id"),
            "name": s.get("name") or '',
            "server": srv,
            "username": u,
            "password": p,
            "max_connections": max_c,
            "active_connections": act_c,
            "free_slots": max(0, max_c - act_c) if max_c else 1,
            "expires_at": s.get("expires_at"),
            "is_expired": s.get("is_expired"),
        })
    return {"credentials": creds, "count": len(creds)}

@app.patch("/api/sources/{sid}")
def api_patch_source(sid: int, body: dict):
    src = get_source(sid)
    if not src: raise HTTPException(404, "Source not found")
    allowed = {k: v for k, v in body.items() if k in ("name", "priority", "payment_url")}
    if "priority" in allowed:
        try: allowed["priority"] = int(allowed["priority"])
        except (ValueError, TypeError): del allowed["priority"]
    if not allowed: raise HTTPException(400, "Nothing to update")
    update_source(sid, **allowed)
    return {"ok": True}

@app.delete("/api/sources/{sid}")
def api_delete_source(sid: int):
    src = get_source(sid)
    if not src: raise HTTPException(404, "Source not found")
    delete_source(sid)
    return {"ok": True}

@app.post("/api/sources/{sid}/activate")
def api_activate_source(sid: int):
    """Toggle a source active/inactive. Imports channels if activating and none exist."""
    src = get_source(sid)
    if not src: raise HTTPException(404, "Source not found")
    new_state = activate_source(sid)  # returns 1 (activated) or 0 (deactivated)
    if new_state:
        # Check if this source already has channels loaded
        existing = get_all_channels_raw(source_id=sid)
        if not existing:
            try:
                if src["type"] == "xc":
                    channels, default_epg = import_channels_from_xc(
                        src["xc_server"], src["xc_username"], src["xc_password"], src["xc_output"], sid)
                    epg = src.get("epg_url") or default_epg
                    acct = fetch_xc_account_info(src["xc_server"], src["xc_username"], src["xc_password"])
                    update_source(sid, expires_at=acct.get("expires_at"),
                                 max_connections=acct.get("max_connections"),
                                 active_connections=acct.get("active_connections"),
                                 status=acct.get("status",""))
                elif src["type"] == "m3u":
                    channels = import_channels_from_m3u(src["m3u_url"], sid, prefix_chno=_is_hdhr_source(src.get("name","")))
                    epg = src.get("epg_url") or ""
                else:
                    raise HTTPException(400, "Unknown source type")
            except requests.HTTPError as e:
                raise HTTPException(400, f"Fetch failed: {e}") from e
            insert_channels(channels)
            if epg:
                update_source(sid, epg_url=epg)
            # Additive: only group the newly-imported (ungrouped) channels.
            # apply_rules() would re-sweep ALL active channels and yank any
            # that don't match a rule into Unmatched — wiping curation when a
            # second source is activated alongside an existing one.
            _assign_new_channels()
            apply_source_order_to_groups()
            sort_sxm_group()
            _post_import_pipeline(sort_groups=True)
            return {"ok": True, "active": True, "channels": len(channels)}
    return {"ok": True, "active": bool(new_state)}

@app.post("/api/sources/reset")
def api_reset_source():
    """Wipe all groups, rules, and channels, then reimport all active sources from scratch."""
    sids = get_active_source_ids()
    if not sids: raise HTTPException(400, "No active source")
    # Nuke everything
    clear_groups()
    for sid in sids:
        clear_source_channels(sid)
    # Reimport all active sources
    total = 0
    for sid in sids:
        src = get_source(sid)
        if not src: continue
        try:
            if src["type"] == "xc":
                channels, default_epg = import_channels_from_xc(
                    src["xc_server"], src["xc_username"], src["xc_password"], src["xc_output"], sid)
                epg = src.get("epg_url") or default_epg
            elif src["type"] == "m3u":
                channels = import_channels_from_m3u(src["m3u_url"], sid, prefix_chno=_is_hdhr_source(src.get("name","")))
                epg = src.get("epg_url") or ""
            else:
                continue
        except requests.HTTPError:
            continue
        insert_channels(channels)
        if epg:
            update_source(sid, epg_url=epg)
        total += len(channels)
    apply_rules()
    apply_source_order_to_groups()
    sort_sxm_group()
    _post_import_pipeline(sort_groups=True)
    return {"ok": True, "channels": total}

@app.post("/api/sources/{sid}/refresh")
def api_refresh_source(sid: int):
    """Sync a source: fetch fresh channel list, detect additions/removals, preserve user edits."""
    src = get_source(sid)
    if not src: raise HTTPException(404, "Source not found")
    try:
        if src["type"] == "xc":
            channels, default_epg = import_channels_from_xc(
                src["xc_server"], src["xc_username"], src["xc_password"], src["xc_output"], sid)
            epg = src.get("epg_url") or default_epg
            try:
                acct = fetch_xc_account_info(src["xc_server"], src["xc_username"], src["xc_password"])
                update_source(sid, expires_at=acct.get("expires_at"),
                             max_connections=acct.get("max_connections"),
                             active_connections=acct.get("active_connections"),
                             status=acct.get("status",""))
            except Exception:
                pass
        elif src["type"] == "m3u":
            channels = import_channels_from_m3u(src["m3u_url"], sid, prefix_chno=_is_hdhr_source(src.get("name","")))
            epg = src.get("epg_url") or ""
        else:
            raise HTTPException(400, "Unknown source type")
    except requests.RequestException as e:
        raise HTTPException(400, f"Fetch failed: {e}") from e
    result = sync_channels(sid, channels)
    if epg:
        update_source(sid, epg_url=epg)
    update_source(sid, last_refreshed=datetime.now(timezone.utc).isoformat())
    # Store import diff for the source health dashboard
    diff_data = json.dumps({
        "added": result["added"], "updated": result["updated"], "removed": result["removed"],
        "revived": result.get("revived", 0),
        "at": datetime.now(timezone.utc).isoformat(),
    })
    update_source(sid, last_import_diff=diff_data)
    if result["added"] > 0 or result["removed"] > 0:
        _assign_new_channels()
        _post_import_pipeline(sort_groups=False)
    schedule_export()
    return {"ok": True, "added": result["added"], "updated": result["updated"],
            "removed": result["removed"], "revived": result.get("revived", 0)}


# ── Import ────────────────────────────────────────────────────────

@app.post("/api/import/xc")
def api_import_xc(p: ImportXC):
    safe = parse_server(p.server)
    name = p.name.strip() or f"{p.username}@{safe}"
    acct = fetch_xc_account_info(safe, p.username, p.password)
    src = add_xc(name=name, server=safe, username=p.username, password=p.password,
                 output=p.output, epg_url=(p.epg_url or "").strip(),
                 expires_at=acct.get("expires_at"), max_connections=acct.get("max_connections"),
                 active_connections=acct.get("active_connections"), status=acct.get("status",""))
    force_activate_source(src["id"])
    try:
        channels, default_epg = import_channels_from_xc(safe, p.username, p.password, p.output, src["id"])
    except requests.HTTPError as e:
        raise HTTPException(400, f"XC request failed: {e}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    epg = (p.epg_url or "").strip() or default_epg
    update_source(src["id"], epg_url=epg)
    insert_channels(channels)
    # Additive import — group only the new channels, never re-sweep curation.
    _assign_new_channels()
    apply_source_order_to_groups()
    sort_sxm_group()
    _post_import_pipeline(sort_groups=True)
    return {"ok": True, "source_id": src["id"], "channels": len(channels)}

@app.post("/api/import/m3u")
def api_import_m3u(p: ImportM3U):
    name = p.name.strip() or p.m3u_url.strip()[:60]
    epg = (p.epg_url or "").strip()
    src = add_m3u(name=name, m3u_url=p.m3u_url.strip(), epg_url=epg)
    force_activate_source(src["id"])
    try:
        channels = import_channels_from_m3u(p.m3u_url, src["id"], prefix_chno=_is_hdhr_source(name))
    except requests.HTTPError as e:
        raise HTTPException(400, f"M3U request failed: {e}") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    insert_channels(channels)
    # Additive import — group only the new channels, never re-sweep curation.
    _assign_new_channels()
    apply_source_order_to_groups()
    sort_sxm_group()
    _post_import_pipeline(sort_groups=True)
    return {"ok": True, "source_id": src["id"], "channels": len(channels)}


# ── Groups ────────────────────────────────────────────────────────

@app.get("/api/groups")
def api_groups():
    asids = get_active_source_ids()
    groups = list_groups(source_id=asids)
    coverage_map = _get_group_coverage_map()
    for g in groups:
        cov = coverage_map.get(g.get("id") or "", {})
        g["epg_coverage_pct"] = cov.get("coverage_pct")
        g["epg_coverage_estimated"] = cov.get("estimated", False)
        g["epg_channels"] = cov.get("channels", 0)
        g["epg_with_programmes"] = cov.get("with_programmes", 0)
        g["epg_synthetic"] = cov.get("synthetic", 0)
        g["epg_dummy"] = cov.get("dummy", 0)
    total = sum(g.get("channel_count", 0) for g in groups)
    return {"groups": groups, "total_channels": total}


def _group_number_order_health():
    """Compare configured group order with player-visible stable numbers."""
    active_ids = get_active_source_ids()
    groups = sorted(
        list_groups(source_id=active_ids),
        key=lambda g: (0 if g.get("pinned") else 1, g.get("sort_order", 0)),
    )
    selected = _collect_selected_channels(groups, active_ids)
    number_map = assign_export_chnos([ch["id"] for _, ch in selected])
    by_group = {}
    for group, channel in selected:
        number = number_map.get(channel["id"])
        if number is not None:
            by_group.setdefault(group["id"], []).append(number)

    rows = []
    for group in groups:
        numbers = by_group.get(group["id"], [])
        if not numbers:
            continue
        start, end = min(numbers), max(numbers)
        rows.append({
            "id": group["id"], "name": group["name"], "channels": len(numbers),
            "start": start, "end": end,
            "aligned": numbers == sorted(numbers),
        })
    # A newly-added channel keeps existing station numbers stable and receives
    # a new high number. Its group's numeric range can therefore overlap later
    # groups without changing where Dispatcharr places the group; group order
    # is driven by the first/minimum provider number. Only flag an actual start
    # inversion (or an internal channel-number inversion), not cascading range
    # overlap from one appended station.
    for i, row in enumerate(rows):
        prev_start = rows[i - 1]["start"] if i else None
        next_start = rows[i + 1]["start"] if i + 1 < len(rows) else None
        if prev_start is not None and row["start"] <= prev_start:
            row["aligned"] = False
        if next_start is not None and row["start"] >= next_start:
            row["aligned"] = False
    mismatches = [row for row in rows if not row["aligned"]]
    return {
        "aligned": not mismatches,
        "group_count": len(rows),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:20],
        "groups": rows,
    }


@app.get("/api/groups/order-health")
def api_group_order_health():
    # Mutating routes invalidate the response cache; do not periodically redo
    # this all-channel scan while the lineup is unchanged.
    return _cached_response("groups:order_health", ttl_sec=365 * 24 * 3600,
                            build_fn=_group_number_order_health)

@app.post("/api/groups")
def api_create_group(p: GroupCreate):
    result = create_group(p.name.strip() or "New Group", parent_id=p.parent_id or None)
    schedule_export()
    return result

@app.patch("/api/groups/{gid}")
def api_patch_group(gid: str, p: GroupPatch):
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    kw = {}
    if p.name is not None: kw["name"] = p.name.strip() or g["name"]
    if p.enabled is not None: kw["enabled"] = 1 if p.enabled else 0
    if p.pinned is not None: kw["pinned"] = 1 if p.pinned else 0
    if p.teamarr is not None: kw["teamarr"] = 1 if p.teamarr else 0
    if p.name_epg is not None: kw["name_epg"] = 1 if p.name_epg else 0
    if p.category is not None: kw["category"] = p.category.strip()
    if p.export_tag is not None: kw["export_tag"] = p.export_tag.strip()
    if p.parent_id is not None: kw["parent_id"] = p.parent_id.strip() or None
    result = update_group(gid, **kw)
    # If name_epg was just enabled, apply it to all channels in this group
    if p.name_epg:
        _apply_name_epg_for_group(gid)
    schedule_export()
    return result

@app.delete("/api/groups/{gid}")
def api_delete_group(gid: str):
    if not get_group(gid): raise HTTPException(404, "Group not found")
    delete_group(gid)
    schedule_export()
    return {"ok": True}

@app.post("/api/groups/reorder")
def api_reorder_groups(p: GroupReorder):
    reorder_groups(p.group_ids)
    _invalidate_response_cache()
    schedule_export()
    return {"ok": True}

@app.post("/api/groups/{gid}/move-to-top")
def api_move_group_top(gid: str):
    groups = list_groups(source_id=get_active_source_ids())
    ids = [g["id"] for g in groups]
    if gid not in ids: raise HTTPException(404)
    ids.remove(gid); ids.insert(0, gid)
    reorder_groups(ids)
    _invalidate_response_cache()
    schedule_export()
    return {"ok": True}

@app.post("/api/groups/sort-alpha")
def api_sort_groups_alpha():
    """Sort groups alphabetically, respecting priority keywords and pinned groups."""
    _sort_groups_with_priorities()
    schedule_export()
    return {"ok": True}

@app.post("/api/groups/{gid}/apply-name-epg")
def api_group_apply_name_epg(gid: str):
    """Set name-based dummy EPG for all channels in this group."""
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    update_group(gid, name_epg=1)
    _apply_name_epg_for_group(gid)
    schedule_export()
    return {"ok": True}

@app.get("/api/settings/name-epg-keywords")
def api_get_name_epg_keywords():
    raw = get_setting("name_epg_keywords")
    try: return json.loads(raw) if raw else []
    except json.JSONDecodeError: return []

@app.put("/api/settings/name-epg-keywords")
def api_set_name_epg_keywords(body: list[str]):
    set_setting("name_epg_keywords", json.dumps(body))
    return body

@app.get("/api/settings/group-sort-priorities")
def api_get_sort_priorities():
    raw = get_setting("group_sort_priorities")
    try: return json.loads(raw) if raw else []
    except json.JSONDecodeError: return []

@app.put("/api/settings/group-sort-priorities")
def api_set_sort_priorities(body: list[str]):
    set_setting("group_sort_priorities", json.dumps(body))
    return body

@app.get("/api/settings/favorite-team-keywords")
def api_get_team_keywords():
    raw = get_setting("favorite_team_keywords")
    try: return json.loads(raw) if raw else []
    except json.JSONDecodeError: return []

@app.put("/api/settings/favorite-team-keywords")
def api_set_team_keywords(body: list[str]):
    set_setting("favorite_team_keywords", json.dumps(body))
    _cache.pop("team_kw", None)
    return body


@app.get("/api/settings/group-categories")
def api_get_categories():
    raw = get_setting("group_categories")
    try: return json.loads(raw) if raw else []
    except json.JSONDecodeError: return []

@app.put("/api/settings/group-categories")
def api_set_categories(body: list[dict]):
    # Validate: each must have name, color, sort_order
    clean = []
    for i, c in enumerate(body):
        clean.append({"name": str(c.get("name","")), "color": str(c.get("color","#666")), "sort_order": i})
    set_setting("group_categories", json.dumps(clean))
    return clean

@app.post("/api/groups/{gid}/move-to-position")
def api_move_group_to_position(gid: str, body: dict):
    """Move a group to a specific position number (1-based)."""
    pos = body.get("position", 0)
    if pos < 1: raise HTTPException(400, "Position must be >= 1")
    g = get_group(gid)
    if not g: raise HTTPException(404)
    groups = list_groups(source_id=get_active_source_ids())
    ids = [grp["id"] for grp in groups]
    if gid in ids: ids.remove(gid)
    idx = min(pos - 1, len(ids))
    ids.insert(idx, gid)
    reorder_groups(ids)
    _invalidate_response_cache()
    schedule_export()
    return {"ok": True, "position": idx + 1}


def _sort_groups_with_priorities():
    """Sort all groups: pinned first, then priority keyword matches in order, then alphabetical.
    Also auto-adds pinned group names to priority keywords for future loads."""
    groups = list_groups()
    if not groups: return

    raw = get_setting("group_sort_priorities")
    try: priorities = json.loads(raw) if raw else []
    except json.JSONDecodeError: priorities = []

    # Auto-add pinned group names to priorities
    changed = False
    for g in groups:
        if g.get("pinned"):
            name = g.get("name", "")
            if name and not any(p.lower() == name.lower() for p in priorities):
                priorities.append(name)
                changed = True
    if changed:
        set_setting("group_sort_priorities", json.dumps(priorities))

    pinned = [g for g in groups if g.get("pinned")]
    unpinned = [g for g in groups if not g.get("pinned")]

    # Score unpinned: priority match index (lower=higher priority), then alphabetical
    def sort_key(g):
        name_lower = (g.get("name") or "").lower()
        for i, kw in enumerate(priorities):
            if kw.lower() in name_lower:
                return (0, i, name_lower)
        return (1, 0, name_lower)

    # Pinned groups are the user's hand-curated lineup — preserve the manual
    # order (their existing sort_order) instead of re-alphabetizing, otherwise
    # every import shuffles them (e.g. a "Weather" group set above "Antenna"
    # would jump down to alphabetical position on the next source import).
    pinned.sort(key=lambda g: (g.get("sort_order", 0), (g.get("name") or "").lower()))
    unpinned.sort(key=sort_key)

    ordered = pinned + unpinned
    reorder_groups([g["id"] for g in ordered])


def _post_import_pipeline(sort_groups=False):
    """Full pipeline after channels are imported/grouped: optionally sort, auto-flag name EPG, apply name EPG, re-export."""
    if sort_groups:
        _sort_groups_with_priorities()
    maintain_group_archive()
    _auto_apply_name_epg_keywords()
    _apply_name_epg_all()
    _do_background_export()


def _apply_name_epg_for_group(gid):
    """Set dummy tvg_id (based on channel name) for channels that don't have a real EPG match."""
    asid = get_active_source_ids()
    chs, _ = get_group_channels(gid, limit=50000, source_id=asid)
    for ch in chs:
        tvg = (ch.get("tvg_id") or "").strip()
        if not tvg or tvg.startswith("dummy."):
            dummy = _make_dummy_tvg_id(ch.get("name") or "channel")
            update_channel(ch["id"], tvg_id=dummy)


def _apply_name_epg_all():
    """Apply name-based EPG to all groups that have name_epg=1."""
    groups = list_groups()
    asid = get_active_source_ids()
    for g in groups:
        if not g.get("name_epg"):
            continue
        chs, _ = get_group_channels(g["id"], limit=50000, source_id=asid)
        for ch in chs:
            tvg = (ch.get("tvg_id") or "").strip()
            if not tvg or tvg.startswith("dummy."):
                dummy = _make_dummy_tvg_id(ch.get("name") or "channel")
                update_channel(ch["id"], tvg_id=dummy)


def _auto_apply_name_epg_keywords():
    """Auto-set name_epg=1 on groups whose names match name_epg_keywords."""
    raw = get_setting("name_epg_keywords")
    try:
        keywords = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        keywords = []
    if not keywords:
        return
    groups = list_groups()
    for g in groups:
        name_lower = (g.get("name") or "").lower()
        for kw in keywords:
            if kw.lower() in name_lower:
                if not g.get("name_epg"):
                    update_group(g["id"], name_epg=1)
                break


# ── Rules ─────────────────────────────────────────────────────────

@app.post("/api/groups/{gid}/rules")
def api_add_rule(gid: str, p: RuleAdd):
    if not get_group(gid): raise HTTPException(404)
    pattern = p.pattern.strip()
    if not pattern: raise HTTPException(400, "Pattern cannot be empty")
    if p.match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(400, f"Invalid regex: {e}")
    result = add_rule(gid, p.field, pattern, p.match_type)
    schedule_export()
    return result

@app.patch("/api/groups/{gid}/rules/{rid}")
def api_update_rule(gid: str, rid: str, p: RuleAdd):
    if not get_group(gid): raise HTTPException(404)
    rule = get_rule(rid)
    if not rule or rule["group_id"] != gid: raise HTTPException(404)
    pattern = p.pattern.strip()
    if not pattern: raise HTTPException(400, "Pattern cannot be empty")
    if p.match_type == "regex":
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(400, f"Invalid regex: {e}")
    update_rule(rid, field=p.field, pattern=pattern, match_type=p.match_type)
    schedule_export()
    return {**rule, "field": p.field, "pattern": pattern, "match_type": p.match_type}

@app.delete("/api/groups/{gid}/rules/{rid}")
def api_delete_rule(gid: str, rid: str):
    delete_rule(rid)
    schedule_export()
    return {"ok": True}

@app.post("/api/groups/{gid}/rules/preview")
def api_preview_rule(gid: str, p: RuleAdd):
    rule = {"field": p.field, "pattern": p.pattern.strip(), "match_type": p.match_type}
    channels = get_all_channels_raw(source_id=get_active_source_ids())
    matches = [ch for ch in channels if matches_rule(ch, rule)]
    protected = [ch for ch in matches if ch.get("placement_locked")]
    moves = [ch for ch in matches if ch.get("group_id") != gid and not ch.get("placement_locked")]
    already = [ch for ch in matches if ch.get("group_id") == gid]
    group_names = {group["id"]: group["name"] for group in list_groups(source_id=get_active_source_ids())}
    return {
        "match_count": len(matches), "would_move": len(moves),
        "protected": len(protected), "already_here": len(already),
        "sample": [{"id": ch.get("id"), "name": ch.get("name"),
                    "from_group": group_names.get(ch.get("group_id"), "Ungrouped")}
                   for ch in moves[:30]],
    }

@app.post("/api/apply-rules")
def api_apply_rules():
    result = apply_rules()
    if result.get("moves"):
        _post_import_pipeline(sort_groups=False)
    return {"ok": True, **result}

@app.get("/api/apply-rules/preview")
def api_apply_rules_preview():
    return preview_rules()

@app.post("/api/groups/sort-sxm")
def api_sort_sxm():
    """Sort 'Radio | Sirius XM' group by official SiriusXM channel numbers."""
    count = sort_sxm_group()
    if not count:
        raise HTTPException(404, "Radio | Sirius XM group not found or empty")
    schedule_export()
    return {"ok": True, "sorted": count}

@app.post("/api/groups/{gid}/sort-sxm")
def api_sort_sxm_group(gid: str):
    """Sort a specific group by official SiriusXM channel numbers."""
    count = sort_sxm_group(gid)
    if not count:
        raise HTTPException(404, "Group not found or empty")
    schedule_export()
    return {"ok": True, "sorted": count}


# ── Channels ──────────────────────────────────────────────────────

# Lightweight cache for settings that rarely change (team keywords, active sources)
_cache = {}
_cache_ttl = 5  # seconds

def _cached_team_keywords():
    now = time.monotonic()
    if "team_kw" in _cache and now - _cache["team_kw_t"] < _cache_ttl:
        return _cache["team_kw"]
    raw = get_setting("favorite_team_keywords")
    try: kw = json.loads(raw) if raw else []
    except json.JSONDecodeError: kw = []
    _cache["team_kw"] = kw; _cache["team_kw_t"] = now
    return kw

@app.get("/api/groups/{gid}/channels")
def api_group_channels(gid: str, offset: int = Query(0, ge=0),
                       limit: int = Query(100, ge=1, le=500), q: str = Query("")):
    asid = get_active_source_ids()
    chs, total = get_group_channels(gid, offset, limit, q, source_id=asid)
    return {"channels": chs, "total": total, "offset": offset, "limit": limit}


@app.get("/api/groups/{gid}/listings/current")
def api_group_current_listings(gid: str):
    """Compact current/next listings for Channel Editor rows."""
    global _guide_cache, _guide_cache_time
    if not get_group(gid):
        raise HTTPException(404, "Group not found")
    epg_path = EXPORT_DIR / "organized.xml"
    epg_is_newer = epg_path.exists() and epg_path.stat().st_mtime > _guide_cache_time
    if epg_is_newer or _guide_cache is None or (time.time() - _guide_cache_time) > _GUIDE_CACHE_TTL:
        _refresh_guide_cache()
    return _current_group_listings(_guide_cache or {}, gid)

@app.get("/api/channels/search")
def api_search(q: str = Query(""), limit: int = Query(100, ge=1, le=500),
               offset: int = Query(0, ge=0),
               favorites: bool = Query(False), enabled: bool = Query(False),
               no_epg: bool = Query(False), group_id: str = Query("")):
    results, total = search_channels(
        q, limit, offset=offset, favorites_only=favorites, enabled_only=enabled,
        no_epg=no_epg, group_id=group_id or None,
        source_id=get_active_source_ids())
    return {"results": results, "total": total, "offset": offset, "limit": limit}

@app.get("/api/favorites")
def api_favorites():
    return get_favorites(source_id=get_active_source_ids())

# ── Duplicate Channels (must be before /api/channels/{cid}) ──────

@app.get("/api/channels/duplicates")
def api_duplicates():
    """Find channels with the same URL (duplicates across groups)."""
    dupes = find_duplicate_channels(source_id=get_active_source_ids())
    return {"duplicates": dupes, "total": len(dupes)}

@app.get("/api/channels/{cid}")
def api_get_channel(cid: str):
    ch = get_channel(cid)
    if not ch: raise HTTPException(404)
    return ch

@app.patch("/api/channels/{cid}")
def api_patch_channel(cid: str, p: ChannelPatch):
    ch = get_channel(cid)
    if not ch: raise HTTPException(404)
    kw = {}
    if p.name is not None:
        kw["name"] = p.name.strip() or ch["name"]
        kw["name_locked"] = 0 if kw["name"] == (ch.get("source_name") or "") else 1
    if p.tvg_id is not None: kw["tvg_id"] = p.tvg_id.strip()
    if p.tvg_name is not None: kw["tvg_name"] = p.tvg_name.strip()
    if p.enabled is not None: kw["enabled"] = 1 if p.enabled else 0
    if p.favorite is not None: kw["favorite"] = 1 if p.favorite else 0
    if p.logo is not None: kw["logo"] = p.logo.strip()
    if p.placement_locked is not None:
        kw["placement_locked"] = 1 if p.placement_locked else 0
    if p.backup_urls is not None:
        # Store as a JSON array of cleaned, non-empty URLs. Empty list => "".
        cleaned = [u.strip() for u in p.backup_urls if u and u.strip()]
        kw["backup_urls"] = json.dumps(cleaned) if cleaned else ""
    result = update_channel(cid, **kw)
    schedule_export()
    return result

@app.post("/api/channels/{cid}/set-name-epg")
def api_channel_set_name_epg(cid: str):
    """Set this channel's tvg_id to a dummy value based on its name."""
    ch = get_channel(cid)
    if not ch: raise HTTPException(404)
    dummy = _make_dummy_tvg_id(ch.get("name") or "channel")
    result = update_channel(cid, tvg_id=dummy)
    schedule_export()
    return result

@app.post("/api/channels/{cid}/unset-epg")
def api_channel_unset_epg(cid: str):
    """Clear this channel's tvg_id (no EPG)."""
    ch = get_channel(cid)
    if not ch: raise HTTPException(404)
    result = update_channel(cid, tvg_id="")
    schedule_export()
    return result

@app.post("/api/channels/{cid}/restore-epg")
def api_channel_restore_epg(cid: str):
    """Restore this channel's tvg_id to the original value from the source."""
    ch = get_channel(cid)
    if not ch: raise HTTPException(404)
    original = ch.get("original_tvg_id") or ""
    result = update_channel(cid, tvg_id=original)
    schedule_export()
    return result

@app.post("/api/channels/{cid}/move")
def api_move_channel(cid: str, p: ChannelMove):
    if not get_channel(cid): raise HTTPException(404)
    if not get_group(p.to_group_id): raise HTTPException(404, "Group not found")
    move_channel(cid, p.to_group_id)
    schedule_export()
    return {"ok": True}

@app.post("/api/channels/bulk-move")
def api_bulk_move(p: BulkMove):
    if not get_group(p.to_group_id): raise HTTPException(404, "Group not found")
    n = bulk_move_channels(p.channel_ids, p.to_group_id)
    schedule_export()
    return {"ok": True, "moved": n}

@app.post("/api/channels/bulk-copy")
def api_bulk_copy(p: BulkMove):
    if not get_group(p.to_group_id): raise HTTPException(404, "Group not found")
    n = bulk_copy_channels(p.channel_ids, p.to_group_id)
    schedule_export()
    return {"ok": True, "copied": n}

@app.post("/api/channels/bulk-toggle")
def api_bulk_toggle(p: BulkToggle):
    n = bulk_toggle_channels(p.channel_ids, p.enabled)
    schedule_export()
    return {"ok": True, "toggled": n}

@app.post("/api/channels/bulk-favorite")
def api_bulk_fav(p: BulkFavorite):
    n = bulk_favorite_channels(p.channel_ids, p.favorite)
    schedule_export()
    return {"ok": True, "updated": n}

@app.post("/api/channels/bulk-lock")
def api_bulk_lock(p: BulkLock):
    n = bulk_lock_channels(p.channel_ids, p.locked)
    return {"ok": True, "updated": n, "locked": p.locked}

@app.post("/api/channels/bulk-epg")
def api_bulk_epg(p: BulkNameEpg):
    """Apply name-based EPG, unset, or restore original EPG in bulk."""
    updated = 0
    for cid in p.channel_ids:
        ch = get_channel(cid)
        if not ch: continue
        if p.action == "name":
            dummy = _make_dummy_tvg_id(ch.get("name") or "channel")
            update_channel(cid, tvg_id=dummy)
        elif p.action == "unset":
            update_channel(cid, tvg_id="")
        elif p.action == "restore":
            original = ch.get("original_tvg_id") or ""
            update_channel(cid, tvg_id=original)
        updated += 1
    schedule_export()
    return {"ok": True, "updated": updated}

@app.post("/api/channels/bulk-logo")
def api_bulk_logo(p: BulkLogo):
    """Clear or set logos for a batch of channels."""
    updated = 0
    for cid in p.channel_ids:
        if p.action == "clear":
            update_channel(cid, logo="")
        elif p.action == "set" and p.logo_url:
            update_channel(cid, logo=p.logo_url)
        updated += 1
    if updated:
        schedule_export()
    return {"ok": True, "updated": updated}
def api_fix_tvg_names():
    """Prepend channel_number to tvg_name for HDHR source channels only."""
    # Only apply to HDHR sources
    hdhr_sids = [s["id"] for s in list_sources() if _is_hdhr_source(s.get("name",""))]
    if not hdhr_sids:
        return {"ok": True, "fixed": 0}
    channels = get_all_channels_raw(source_id=hdhr_sids)
    fixed = 0
    for ch in channels:
        chno = (ch.get("channel_number") or "").strip()
        tvg = (ch.get("tvg_name") or "").strip()
        if chno and tvg and not tvg.startswith(chno):
            update_channel(ch["id"], tvg_name=f"{chno} {tvg}")
            fixed += 1
    if fixed:
        schedule_export()
    return {"ok": True, "fixed": fixed}

@app.delete("/api/channels/{cid}")
def api_delete_channel(cid: str):
    ch = get_channel(cid)
    if not ch: raise HTTPException(404, "Channel not found")
    delete_channel(cid)
    schedule_export()
    return {"ok": True}

@app.post("/api/channels/bulk-delete")
def api_bulk_delete(p: BulkDelete):
    n = delete_channels_bulk(p.channel_ids)
    schedule_export()
    return {"ok": True, "deleted": n}

@app.post("/api/groups/{gid}/reorder-channels")
def api_reorder_channels(gid: str, p: ReorderChannels):
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    reorder_group_channels(gid, p.channel_ids)
    schedule_export()
    return {"ok": True}

@app.post("/api/groups/{gid}/renumber-block")
def api_renumber_block(gid: str, anchor: int | None = None):
    """Reflow this group's channels (in their current display/sort order) into a
    contiguous ascending block of stable channel numbers, so the order holds in
    players that sort by channel number. See db.renumber_group_block.

    `anchor` is an optional one-off override for where the block should start —
    normally unnecessary (it self-anchors near the group's existing numbers),
    but useful to manually pull a group back to a sane neighborhood if its
    stored numbers are themselves in a bad state."""
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    chs, _ = get_group_channels(gid, limit=50000)
    mapping = renumber_group_block([c["id"] for c in chs], anchor_hint=anchor)
    schedule_export()
    return {"ok": True, "assigned": len(mapping),
            "start": min(mapping.values()) if mapping else None}

@app.post("/api/groups/renumber-all")
def api_renumber_all():
    """One-time GLOBAL renumber: wipes chno_map and reassigns sequential 1..N
    stable channel numbers strictly in (pinned DESC, sort_order ASC) group
    order, then channel sort_order within each group -- exactly the same
    order/selection the live export uses. This is what makes a player that
    sorts purely by channel number (e.g. Dispatcharr in provider-numbering
    mode) actually display every group in the order configured here, instead
    of whatever order numbers happened to be handed out historically.

    Deliberate/explicit action only -- every channel's number can change, so
    any XC client (TiviMate, Televizo, etc.) with a cached playlist needs a
    one-time playlist re-add afterward to pick up the new numbers."""
    asid = get_active_source_ids()
    groups = list_groups(source_id=asid)
    ordered_groups = sorted(groups, key=lambda g: (0 if g.get("pinned") else 1, g.get("sort_order", 0)))
    selected = _collect_selected_channels(ordered_groups, asid)
    mapping = renumber_all_in_order([ch["id"] for _, ch in selected])
    _invalidate_response_cache()
    schedule_export()
    return {"ok": True, "assigned": len(mapping), "groups": len(ordered_groups)}

@app.get("/api/groups/{gid}/channel-priorities")
def api_get_channel_priorities(gid: str):
    raw = get_setting(f"channel_priorities_{gid}")
    try: return json.loads(raw) if raw else []
    except json.JSONDecodeError: return []

@app.put("/api/groups/{gid}/channel-priorities")
def api_set_channel_priorities(gid: str, body: list[str]):
    set_setting(f"channel_priorities_{gid}", json.dumps(body))
    return body

@app.post("/api/groups/{gid}/sort-channels-by-priority")
def api_sort_channels_by_priority(gid: str):
    """Sort channels within a group by priority keywords, then alphabetically."""
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    raw = get_setting(f"channel_priorities_{gid}")
    try: priorities = json.loads(raw) if raw else []
    except json.JSONDecodeError: priorities = []
    asid = get_active_source_ids()
    chs, _ = get_group_channels(gid, limit=50000, source_id=asid)
    if not chs: return {"ok": True}

    def sort_key(ch):
        name_lower = (ch.get("name") or "").lower()
        for i, kw in enumerate(priorities):
            if kw.lower() in name_lower:
                return (0, i, name_lower)
        return (1, 0, name_lower)

    chs.sort(key=sort_key)
    reorder_group_channels(gid, [ch["id"] for ch in chs])
    schedule_export()
    return {"ok": True, "sorted": len(chs)}

@app.post("/api/groups/{gid}/sort-alpha")
def api_sort_channels_alpha(gid: str):
    """Sort channels within a group alphabetically using natural sort (numbers compared numerically)."""
    import re as _re
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    asid = get_active_source_ids()
    chs, _ = get_group_channels(gid, limit=50000, source_id=asid)
    if not chs: return {"ok": True}

    def natural_key(ch):
        name = (ch.get("name") or "").lower()
        return [int(t) if t.isdigit() else t for t in _re.split(r"(\d+)", name)]

    chs.sort(key=natural_key)
    reorder_group_channels(gid, [ch["id"] for ch in chs])
    schedule_export()
    return {"ok": True, "sorted": len(chs)}

@app.post("/api/groups/{gid}/sort-by-source")
def api_sort_channels_by_source(gid: str):
    """Sort channels by source priority, then by channel_number (natural) within each source, then name."""
    import re as _re
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    asid = get_active_source_ids()
    chs, _ = get_group_channels(gid, limit=50000, source_id=asid)
    if not chs: return {"ok": True}

    src_priority = {s["id"]: (s.get("priority") or 10) for s in list_sources()}

    def nat(s):
        return [int(t) if t.isdigit() else t for t in _re.split(r"(\d+)", (s or "").lower())]

    def sort_key(ch):
        prio = src_priority.get(ch.get("source_id"), 10)
        chno = (ch.get("channel_number") or "").strip()
        # Channels with a number sort before channels without
        has_chno = 0 if chno else 1
        return (prio, has_chno) + tuple(nat(chno) if chno else nat(ch.get("name")))

    chs.sort(key=sort_key)
    reorder_group_channels(gid, [ch["id"] for ch in chs])
    schedule_export()
    return {"ok": True, "sorted": len(chs)}

@app.post("/api/groups/{gid}/channels/bulk-move-to-top")
def api_channels_bulk_move_to_top(gid: str, p: ReorderChannels):
    """Move multiple channels to the top of their group, preserving their relative order."""
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    asid = get_active_source_ids()
    chs, _ = get_group_channels(gid, limit=50000, source_id=asid)
    ids = [c["id"] for c in chs]
    sel = [cid for cid in p.channel_ids if cid in ids]
    rest = [cid for cid in ids if cid not in sel]
    reorder_group_channels(gid, sel + rest)
    schedule_export()
    return {"ok": True}

@app.post("/api/groups/{gid}/channels/{cid}/move-to-top")
def api_channel_move_to_top(gid: str, cid: str):
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    ch = get_channel(cid)
    if not ch or ch.get("group_id") != gid: raise HTTPException(404, "Channel not found in group")
    asid = get_active_source_ids()
    chs, _ = get_group_channels(gid, limit=50000, source_id=asid)
    ids = [c["id"] for c in chs]
    if cid in ids:
        ids.remove(cid)
        ids.insert(0, cid)
    reorder_group_channels(gid, ids)
    schedule_export()
    return {"ok": True}


# ── Export Status ─────────────────────────────────────────────────

@app.get("/api/export-status")
def api_export_status():
    """Return the current export status for frontend polling."""
    return _export_status

@app.get("/api/export/history")
def api_export_history(limit: int = Query(50, ge=1, le=200)):
    """Return the last N export records."""
    return _cached_response(f"dashboard:export_history:{limit}", ttl_sec=8.0,
                            build_fn=lambda: get_export_history(limit))


# ── Channel Health Check ──────────────────────────────────────────

@app.post("/api/channels/health-check")
async def api_health_check(p: HealthCheckRequest):
    """HEAD-check a batch of channel URLs and store the results."""
    import asyncio, httpx
    cids = p.channel_ids[:max(1, min(p.limit, 200))]
    if not cids:
        return {"ok": True, "checked": 0}

    # Fetch channel records
    channels = [get_channel(cid) for cid in cids]
    channels = [c for c in channels if c and c.get("url")]

    async def _check(ch):
        url = ch["url"]
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                r = await client.head(url, headers={"User-Agent": "M3UBoss/2.0"})
            latency = int((time.monotonic() - t0) * 1000)
            set_channel_health(ch["id"], status_code=r.status_code, latency_ms=latency, error=None)
        except Exception as exc:
            latency = int((time.monotonic() - t0) * 1000)
            err = str(exc)[:120]
            set_channel_health(ch["id"], status_code=None, latency_ms=latency, error=err)

    await asyncio.gather(*[_check(ch) for ch in channels])
    return {"ok": True, "checked": len(channels)}

@app.get("/api/channels/health")
def api_get_health(channel_ids: str = Query("")):
    """Return stored health results. Pass comma-separated IDs or omit for all."""
    ids = [x.strip() for x in channel_ids.split(",") if x.strip()] if channel_ids else None
    return get_channel_health(ids)

@app.delete("/api/channels/health")
def api_clear_health():
    clear_channel_health()
    return {"ok": True}


# ── EPG Coverage ──────────────────────────────────────────────────

@app.get("/api/epg/coverage")
def api_epg_coverage():
    """Return EPG coverage statistics plus a real export integrity audit."""
    def _build():
        base = get_epg_coverage()
        audit = _audit_epg_exports()
        result = {**base, **audit}

        issues = []
        if base.get("unmatched"):
            issues.append(f"{base['unmatched']} channel(s) have no EPG")
        if base.get("dummy"):
            issues.append(f"{base['dummy']} channel(s) still use dummy guide data")
        if audit.get("missing_playlist_tvg_ids"):
            issues.append(f"{audit['missing_playlist_tvg_ids']} exported channel(s) have no tvg-id")
        if audit.get("missing_in_xml"):
            issues.append(f"{audit['missing_in_xml']} playlist id(s) are missing from XMLTV")
        if audit.get("no_programmes"):
            issues.append(f"{audit['no_programmes']} channel(s) have no programme rows")
        if audit.get("collisions"):
            issues.append(f"{audit['collisions']} shared tvg-id collision(s) detected")
        if not audit.get("well_formed"):
            issues.append("XMLTV export is not valid XML")
        elif not audit.get("well_ordered"):
            issues.append("XMLTV ordering may confuse players like TiviMate")

        perfect = bool(result.get("total")) and not issues
        result["perfect"] = perfect
        result["issues"] = issues

        if perfect:
            result["status"] = "perfect"
            result["summary"] = "EPG looks perfect — every exported channel has a real guide and matching programme data."
        elif result.get("total", 0) == 0:
            result["status"] = "warning"
            result["summary"] = "No channels are currently available to audit."
        elif audit.get("status") == "error" and audit.get("summary"):
            result["status"] = "error"
            result["summary"] = audit["summary"]
        else:
            result["status"] = "warning"
            result["summary"] = "Needs attention: " + "; ".join(issues[:4]) + ("." if len(issues) <= 4 else " …")
        return result

    return _cached_response("dashboard:epg_coverage", ttl_sec=365 * 24 * 3600, build_fn=_build)


@app.get("/api/epg/watchdog")
def api_epg_watchdog_status():
    """Return the latest automatic EPG watchdog status."""
    return _epg_watchdog_status


@app.get("/api/epg/drift")
def api_epg_drift():
    """Focused EPG-drift view from the periodic watchdog: channel-number drift
    (the cause of guide-on-wrong-channel), duplicate numbers, and how far ahead
    the guide reaches (staleness)."""
    s = _epg_watchdog_status
    return {
        "last_check": s.get("last_check"),
        "epg_fresh": s.get("epg_fresh"),
        "drift_alerts": s.get("drift_alerts", []),
        "drift": s.get("drift", {}),
        "epg_freshness": s.get("epg_freshness", {}),
    }


@app.post("/api/epg/drift/check")
def api_epg_drift_check():
    """Force an immediate watchdog/drift run (instead of waiting for the 15-min
    cadence) and return the fresh drift view."""
    _maybe_run_epg_watchdog(force=True)
    return api_epg_drift()


@app.post("/api/notify/test")
def api_notify_test():
    """Send a test push through the configured ntfy notifier, so you can confirm
    homelab alerts reach your phone."""
    if get_setting("ntfy_enabled") != "1":
        return {"ok": False, "reason": "ntfy not enabled in settings"}
    if not ((get_setting("ntfy_url") or "").strip() and (get_setting("ntfy_topic") or "").strip()):
        return {"ok": False, "reason": "ntfy_url / ntfy_topic not set"}
    _notify("M3U Boss: test notification",
            "If you're seeing this, homelab push alerts are working.",
            priority="default", tags="white_check_mark")
    return {"ok": True, "url": get_setting("ntfy_url"), "topic": get_setting("ntfy_topic")}


@app.post("/api/epg/watchdog/run")
def api_epg_watchdog_run():
    """Run an immediate EPG watchdog audit + self-heal pass."""
    return _run_epg_watchdog_check()


@app.get("/api/epg/dashboard")
def api_epg_dashboard():
    """Guide integrity dashboard with per-group scores and top bad mappings."""
    def _build():
        idx = _build_export_programme_index()
        ids = idx["ids"]
        counts = idx["counts"]
        blank = idx["blank_title"]
        synthetic = idx["synthetic"]
        dummy = idx["dummy"]

        groups = {}
        for tvg_id, meta in ids.items():
            g = meta.get("group") or "Ungrouped"
            row = groups.setdefault(g, {
                "group": g,
                "channels": 0,
                "with_programmes": 0,
                "without_programmes": 0,
                "blank_title": 0,
                "synthetic": 0,
                "dummy": 0,
                "score": 0,
            })
            row["channels"] += 1
            if counts.get(tvg_id, 0) > 0:
                row["with_programmes"] += 1
            else:
                row["without_programmes"] += 1
            if blank.get(tvg_id, 0) > 0:
                row["blank_title"] += 1
            if synthetic.get(tvg_id, 0) > 0:
                row["synthetic"] += 1
            if dummy.get(tvg_id, 0) > 0:
                row["dummy"] += 1

        for g in groups.values():
            total = max(1, g["channels"])
            real = g["with_programmes"] - g["synthetic"] - g["dummy"]
            score = int(max(0, min(100, (real / total) * 100)))
            g["score"] = score

        all_channels = get_all_channels_raw(source_id=get_active_source_ids())
        bad = []
        for ch in all_channels:
            if not ch.get("enabled", True):
                continue
            tvg_id = (ch.get("tvg_id") or "").strip()
            orig = (ch.get("original_tvg_id") or "").strip()
            if not orig or tvg_id == orig:
                continue
            if _suspicious_tvg_id(tvg_id):
                bad.append({
                    "channel_id": ch.get("id"),
                    "name": ch.get("name") or "Unnamed",
                    "group": ch.get("source_group") or "Ungrouped",
                    "tvg_id": tvg_id,
                    "original_tvg_id": orig,
                })

        return {
            "groups": sorted(groups.values(), key=lambda x: (x["score"], -x["channels"])),
            "top_bad_mappings": bad[:50],
            "totals": {
                "groups": len(groups),
                "channels": len(ids),
                "with_programmes": sum(1 for k in ids if counts.get(k, 0) > 0),
                "blank_title": sum(1 for k in ids if blank.get(k, 0) > 0),
            },
        }

    return _cached_response("dashboard:epg_board", ttl_sec=365 * 24 * 3600, build_fn=_build)


@app.get("/api/epg/stale")
def api_epg_stale(limit: int = Query(500, ge=1, le=5000)):
    """List exported channels whose guide has no real programmes in the
    active window (~next 24h) — i.e. empty, synthetic, or dummy-only guides."""
    def _build():
        idx = _build_export_programme_index()
        ids = idx["ids"]
        counts = idx["counts"]
        synthetic = idx["synthetic"]
        dummy = idx["dummy"]
        stale = []
        for tvg_id, meta in ids.items():
            real = (counts.get(tvg_id, 0)
                    - synthetic.get(tvg_id, 0)
                    - dummy.get(tvg_id, 0))
            if real > 0:
                continue
            if dummy.get(tvg_id, 0) > 0:
                kind = "dummy"
            elif synthetic.get(tvg_id, 0) > 0:
                kind = "synthetic"
            else:
                kind = "empty"
            stale.append({
                "tvg_id": tvg_id,
                "name": meta.get("name") or "Unnamed",
                "group": meta.get("group") or "Ungrouped",
                "kind": kind,
            })
        stale.sort(key=lambda s: (s["group"].lower(), s["name"].lower()))
        by_group = {}
        for s in stale:
            by_group[s["group"]] = by_group.get(s["group"], 0) + 1
        return {
            "stale": stale[:limit],
            "total_stale": len(stale),
            "total_exported": len(ids),
            "by_group": sorted(by_group.items(), key=lambda x: -x[1]),
        }

    return _cached_response("dashboard:epg_stale", ttl_sec=365 * 24 * 3600, build_fn=_build)


@app.get("/api/epg/explain/{channel_id}")
def api_epg_explain(channel_id: str):
    ch = get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "Channel not found")

    idx = _build_export_programme_index()
    selected = _collect_selected_channels(sorted(list_groups(source_id=get_active_source_ids()), key=lambda g: (0 if g.get("pinned") else 1, g.get("sort_order", 0))), get_active_source_ids())
    export_map = _build_export_tvg_map(selected)
    export_id = (export_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
    count = idx["counts"].get(export_id, 0)
    reason = []
    if export_id == (ch.get("original_tvg_id") or "").strip() and export_id:
        reason.append("using original provider tvg-id")
    elif export_id == (ch.get("tvg_id") or "").strip() and export_id:
        reason.append("using current channel tvg-id")
    else:
        reason.append("using scoped export tvg-id")
    if idx["synthetic"].get(export_id, 0) > 0:
        reason.append("synthetic event fallback in use")
    if idx["dummy"].get(export_id, 0) > 0:
        reason.append("dummy fallback in use")
    if count == 0:
        reason.append("no guide rows in active window")

    return {
        "channel": {
            "id": ch.get("id"),
            "name": ch.get("name"),
            "group": ch.get("source_group"),
            "tvg_id": ch.get("tvg_id"),
            "original_tvg_id": ch.get("original_tvg_id"),
            "export_tvg_id": export_id,
        },
        "programmes_in_window": count,
        "reason": reason,
    }


@app.post("/api/epg/repair-pack")
def api_epg_repair_pack(body: EpgRepairPackRequest):
    channels = get_all_channels_raw(source_id=get_active_source_ids())
    candidates = []
    for ch in channels:
        if not ch.get("enabled", True):
            continue
        if body.group_name and (ch.get("source_group") or "") != body.group_name:
            continue
        candidates.append(ch)

    actions = []
    for ch in candidates:
        cid = ch.get("id")
        tvg = (ch.get("tvg_id") or "").strip()
        orig = (ch.get("original_tvg_id") or "").strip()
        if body.clear_dummy and (tvg.startswith("dummy-") or tvg.startswith("dummy.")):
            actions.append((cid, ""))
            continue
        if body.fix_suspicious and _suspicious_tvg_id(tvg) and orig:
            actions.append((cid, orig))
            continue
        if body.restore_originals and orig and tvg and tvg != orig:
            actions.append((cid, orig))

    updated = 0
    if not body.dry_run:
        for cid, new_tvg in actions:
            update_channel(cid, tvg_id=new_tvg)
            updated += 1
        if updated:
            schedule_export()

    return {
        "dry_run": body.dry_run,
        "candidates": len(candidates),
        "proposed": len(actions),
        "updated": updated,
        "sample": [{"channel_id": cid, "new_tvg_id": tvg} for cid, tvg in actions[:30]],
    }


@app.get("/api/epg/diff")
def api_epg_diff():
    m3u_cur = EXPORT_DIR / "organized.m3u"
    xml_cur = EXPORT_DIR / "organized.xml"
    m3u_prev = EXPORT_DIR / "organized.m3u.prev"
    xml_prev = EXPORT_DIR / "organized.xml.prev"
    if not m3u_prev.exists() or not xml_prev.exists() or not m3u_cur.exists() or not xml_cur.exists():
        return {"status": "unavailable", "summary": "No previous export snapshot available yet."}
    signature = (_file_signature(m3u_cur), _file_signature(xml_cur),
                 _file_signature(m3u_prev), _file_signature(xml_prev))
    if _epg_diff_cache.get("signature") == signature:
        return copy.deepcopy(_epg_diff_cache["value"])

    def _ids(path):
        out = set()
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.startswith("#EXTINF"):
                continue
            attrs, _ = parse_extinf_attrs(line)
            tvg = (attrs.get("tvg-id") or "").strip()
            if tvg:
                out.add(tvg)
        return out

    def _prog_counts(path):
        signature = _file_signature(path)
        cached = _epg_count_cache.get(signature)
        if cached is not None:
            return cached
        out = {}
        try:
            for _e, elem in ET.iterparse(str(path), events=("end",)):
                tag = elem.tag.split("}", 1)[-1]
                if tag == "programme":
                    cid = (elem.attrib.get("channel") or "").strip()
                    if cid:
                        out[cid] = out.get(cid, 0) + 1
                elem.clear()
        except ET.ParseError:
            return {}
        _epg_count_cache.clear()
        _epg_count_cache[signature] = out
        return out

    cur_ids = _ids(m3u_cur)
    prev_ids = _ids(m3u_prev)
    # Current counts are already part of the shared export snapshot. Only the
    # previous file needs a lightweight scan, once per previous-file signature.
    cur_counts = _get_epg_snapshot()["total_counts"]
    prev_counts = _prog_counts(xml_prev)

    added = sorted(cur_ids - prev_ids)
    removed = sorted(prev_ids - cur_ids)
    changed = []
    for cid in sorted(cur_ids & prev_ids):
        c = cur_counts.get(cid, 0)
        p = prev_counts.get(cid, 0)
        if c != p:
            changed.append({"tvg_id": cid, "prev_programmes": p, "cur_programmes": c})

    result = {
        "status": "ok",
        "added_ids": len(added),
        "removed_ids": len(removed),
        "changed_programme_counts": len(changed),
        "sample_added": added[:30],
        "sample_removed": removed[:30],
        "sample_changed": changed[:30],
    }
    _epg_diff_cache.clear()
    _epg_diff_cache.update(signature=signature, value=copy.deepcopy(result))
    return result


@app.post("/api/rules/sandbox")
def api_rules_sandbox(body: RuleSandboxRequest):
    rule = {
        "field": body.field,
        "pattern": body.pattern,
        "match_type": body.match_type,
    }
    channels = get_all_channels_raw(source_id=get_active_source_ids())
    matches = []
    for ch in channels:
        if body.source_group and (ch.get("source_group") or "") != body.source_group:
            continue
        if matches_rule(ch, rule):
            matches.append({
                "id": ch.get("id"),
                "name": ch.get("name"),
                "source_group": ch.get("source_group"),
            })
    return {
        "count": len(matches),
        "sample": matches[: max(1, min(body.limit, 200))],
    }


@app.get("/api/sources/reliability")
def api_sources_reliability():
    def _build():
        idx = _build_export_programme_index()
        counts = idx["counts"]
        synthetic = idx["synthetic"]
        dummy = idx["dummy"]

        asid = get_active_source_ids()
        groups = sorted(list_groups(source_id=asid), key=lambda g: (0 if g.get("pinned") else 1, g.get("sort_order", 0)))
        selected = _collect_selected_channels(groups, asid)
        export_map = _build_export_tvg_map(selected)

        per_source = {}
        for _, ch in selected:
            sid = ch.get("source_id")
            if sid is None:
                continue
            exp = (export_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
            row = per_source.setdefault(sid, {"total": 0, "with_programmes": 0, "synthetic": 0, "dummy": 0})
            row["total"] += 1
            if counts.get(exp, 0) > 0:
                row["with_programmes"] += 1
            if synthetic.get(exp, 0) > 0:
                row["synthetic"] += 1
            if dummy.get(exp, 0) > 0:
                row["dummy"] += 1

        sources = list_sources()
        out = []
        for s in sources:
            sid = s.get("id")
            stats = per_source.get(sid, {"total": 0, "with_programmes": 0, "synthetic": 0, "dummy": 0})
            total = max(1, stats["total"])
            coverage = stats["with_programmes"] / total
            penalty = (stats["synthetic"] + stats["dummy"]) / total
            score = int(max(0, min(100, (coverage * 100) - (penalty * 25))))
            out.append({
                "id": sid,
                "name": s.get("name"),
                "is_active": bool(s.get("is_active")),
                "channels": stats["total"],
                "coverage": round(coverage * 100, 2),
                "synthetic": stats["synthetic"],
                "dummy": stats["dummy"],
                "score": score,
            })
        out.sort(key=lambda x: x["score"], reverse=True)
        return out

    return _cached_response("dashboard:sources_reliability", ttl_sec=20.0, build_fn=_build)


# ── Group Templates ───────────────────────────────────────────────

@app.get("/api/groups/templates")
def api_list_templates():
    return list_group_templates()

@app.post("/api/groups/templates")
def api_save_template(p: GroupTemplateSave):
    name = p.name.strip()
    if not name: raise HTTPException(400, "Template name required")
    if p.group_id:
        g = get_group(p.group_id)
        if not g: raise HTTPException(404, "Group not found")
        rules = get_group_rules(p.group_id)
        # Strip IDs from rules so they get new IDs when applied
        rules_clean = [{"field": r["field"], "pattern": r["pattern"],
                        "match_type": r["match_type"]} for r in rules]
        tpl = save_group_template(
            name=name,
            category=g.get("category") or "",
            enabled=g.get("enabled", 1),
            pinned=g.get("pinned", 0),
            teamarr=g.get("teamarr", 0),
            name_epg=g.get("name_epg", 0),
            export_tag=g.get("export_tag") or "",
            rules=rules_clean,
        )
    else:
        tpl = save_group_template(name=name)
    return tpl

@app.delete("/api/groups/templates/{tid}")
def api_delete_template(tid: str):
    if not get_group_template(tid): raise HTTPException(404)
    delete_group_template(tid)
    return {"ok": True}

@app.post("/api/groups/{gid}/apply-template")
def api_apply_template(gid: str, body: dict):
    tid = body.get("template_id", "")
    if not tid: raise HTTPException(400, "template_id required")
    g = get_group(gid)
    if not g: raise HTTPException(404, "Group not found")
    tpl = get_group_template(tid)
    if not tpl: raise HTTPException(404, "Template not found")
    # Apply settings
    update_group(gid,
        category=tpl.get("category") or "",
        enabled=tpl.get("enabled", 1),
        pinned=tpl.get("pinned", 0),
        teamarr=tpl.get("teamarr", 0),
        name_epg=tpl.get("name_epg", 0),
        export_tag=tpl.get("export_tag") or "",
    )
    # Apply rules: delete existing, add template rules
    existing_rules = get_group_rules(gid)
    for r in existing_rules:
        delete_rule(r["id"])
    for r in tpl.get("rules", []):
        pattern = (r.get("pattern") or "").strip()
        if pattern:
            add_rule(gid, r.get("field", "source_group"), pattern,
                     r.get("match_type", "contains"))
    schedule_export()
    return get_group(gid)


# ── Bulk Rename ───────────────────────────────────────────────────

@app.post("/api/channels/bulk-rename")
def api_bulk_rename(p: BulkRename):
    """Find/replace in channel names. Supports literal and regex."""
    pattern = p.pattern.strip()
    if not pattern: raise HTTPException(400, "Pattern is required")
    if p.is_regex:
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(400, f"Invalid regex: {e}")
    count = bulk_rename_channels(pattern, p.replacement, is_regex=p.is_regex,
                                 source_id=get_active_source_ids())
    if count:
        schedule_export()
    return {"ok": True, "modified": count}

@app.post("/api/channels/bulk-rename/preview")
def api_bulk_rename_preview(p: BulkRename):
    """Preview how many channels would be affected by a rename."""
    pattern = p.pattern.strip()
    if not pattern: raise HTTPException(400, "Pattern is required")
    if p.is_regex:
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(400, f"Invalid regex: {e}")
    channels = get_all_channels_raw(source_id=get_active_source_ids())
    examples = []
    count = 0
    for ch in channels:
        name = ch.get("name", "")
        if p.is_regex:
            new_name = re.sub(pattern, p.replacement, name, flags=re.IGNORECASE)
        else:
            idx = name.lower().find(pattern.lower())
            if idx == -1:
                continue
            new_name = name[:idx] + p.replacement + name[idx+len(pattern):]
        if new_name != name:
            count += 1
            if len(examples) < 10:
                examples.append({"old": name, "new": new_name})
    return {"count": count, "examples": examples}


# ── EPG Sources ───────────────────────────────────────────────────

@app.get("/api/epg-sources")
def api_epg_sources():
    return list_epg_sources()

@app.post("/api/epg-sources")
def api_add_epg(p: EPGSourceAdd):
    return add_epg_source(p.name.strip() or "EPG Source", p.url.strip())

@app.delete("/api/epg-sources/{eid}")
def api_del_epg(eid: int):
    delete_epg_source(eid)
    return {"ok": True}

@app.patch("/api/epg")
def api_patch_epg(p: EPGPatch):
    # Update EPG URL on the first active source (primary)
    active = get_active_source()
    if active:
        update_source(active["id"], epg_url=p.epg_url.strip())
    return {"ok": True}


# ── Suggested EPG Sources ─────────────────────────────────────────

_SUGGESTED_EPG = [
    {"name": "US Locals & Cable (epg.pw)", "url": "https://epg.pw/xmltv/epg_US.xml.gz",
     "description": "5,400+ US channels incl. locals (KDKA, WTAE, WPXI, WQED, etc.) & cable — updated daily"},
    {"name": "US PBS Stations (mjh.nz)", "url": "https://i.mjh.nz/PBS/all.xml.gz",
     "description": "US PBS local stations (WQED, WETA, KQED, etc.) — all PBS affiliates"},
    {"name": "Pluto TV US (mjh.nz)", "url": "https://i.mjh.nz/PlutoTV/us.xml.gz",
     "description": "Pluto TV free streaming channels — US lineup"},
    {"name": "Plex FAST US (mjh.nz)", "url": "https://i.mjh.nz/Plex/us.xml.gz",
     "description": "Plex free ad-supported streaming channels — US lineup"},
    {"name": "Samsung TV Plus US (mjh.nz)", "url": "https://i.mjh.nz/SamsungTVPlus/us.xml.gz",
     "description": "Samsung TV Plus free streaming channels — US lineup"},
]

@app.get("/api/epg/diagnostics")
def api_epg_diagnostics():
    """Health + match-coverage report for EPG.

    - ``match``: how many exported channels got real guide data last export,
      plus a sample of the ones that didn't (so you can see WHAT is missing).
    - ``sources``: per-EPG-URL fetch health from the last export, including any
      source that errored and was skipped (instead of silently dropped).
    """
    with _epg_match_stats_lock:
        match = dict(_epg_match_stats)
    with _epg_export_status_lock:
        export_sources = [dict(v) for v in _epg_export_status.values()]
    # Fold in browse-fetch status for any URL not seen during export.
    seen = {s.get("url") for s in export_sources}
    for url, st in list(_epg_source_status.items()):
        if url not in seen:
            export_sources.append({"url": url, **{k: st.get(k) for k in
                                   ("status", "channels", "error")}})
    return {"match": match, "sources": export_sources}


@app.get("/api/logos/coverage")
def api_logo_coverage():
    """Match-logo coverage from the last export: how many distinct matchup
    titles resolved to a composite badge, and a sample of the ones that were
    skipped with the reason (team couldn't be resolved). Run an export first
    if this is empty."""
    with _logo_coverage_lock:
        return dict(_logo_coverage)


@app.post("/api/logos/override")
def api_logos_override(p: LogoOverride):
    """Manually pin a team → badge image URL for cases TheSportsDB can't find
    or gets wrong. Takes effect on the next export (clears the cached failure
    for that team automatically)."""
    try:
        from app import match_logos
        ok = match_logos.set_override(p.team, p.sport, p.badge_url)
        if not ok:
            raise HTTPException(400, "team, sport and badge_url are all required")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"override failed: {e}") from e


@app.post("/api/logos/clear-cache")
def api_logos_clear_cache(negatives_only: bool = Query(True)):
    """Clear the match-logo resolver cache so failed lookups get retried.

    negatives_only=true (default) drops only the cached *failures* (teams that
    didn't resolve, e.g. a new franchise TheSportsDB hadn't indexed yet) and
    keeps the built composites. negatives_only=false also forgets resolved
    badges (composite PNGs on disk are left in place and rebuilt on demand)."""
    try:
        from app import match_logos
        removed = match_logos.clear_cache(negatives_only=negatives_only)
        return {"ok": True, "removed": removed, "negatives_only": negatives_only}
    except Exception as e:
        raise HTTPException(500, f"clear-cache failed: {e}") from e


@app.get("/api/epg-sources/suggested")
def api_suggested_epg():
    """Return suggested public EPG sources, excluding any already added."""
    existing_urls = set()
    for src in get_active_sources():
        u = (src.get("epg_url") or "").strip()
        if u: existing_urls.add(u)
    for es in list_epg_sources():
        existing_urls.add(es["url"])
    return [s for s in _SUGGESTED_EPG if s["url"] not in existing_urls]


# ── EPG Browse / Search ──────────────────────────────────────────

_epg_channel_cache: list[dict] = []
_epg_channel_cache_time: float = 0
_EPG_CHANNEL_CACHE_TTL = 3600  # 1 hour
# Per-source status: url -> {status, channels, fetched_at, error}
_epg_source_status: dict[str, dict] = {}

def _build_epg_channel_list() -> list[dict]:
    """Fetch all EPG sources and build a searchable list of EPG channel entries."""
    global _epg_source_status
    epg_urls = []
    url_names = {}  # url -> friendly name
    for src in get_active_sources():
        u = (src.get("epg_url") or "").strip()
        if u and u not in epg_urls:
            epg_urls.append(u)
            url_names[u] = src.get("name") or "Active Source"
    for es in list_epg_sources():
        if es.get("url") and es["url"] not in epg_urls:
            epg_urls.append(es["url"])
            url_names[es["url"]] = es.get("name") or es["url"]

    channels = []
    seen_ids = set()
    for url in epg_urls:
        source_name = url_names.get(url, url)
        src_count = 0
        try:
            root = parse_epg(url)
            for node in root.findall("channel"):
                cid = node.attrib.get("id", "")
                if not cid or cid in seen_ids:
                    continue
                seen_ids.add(cid)
                src_count += 1
                dn = node.find("display-name")
                display = (dn.text or "") if dn is not None else ""
                icon_node = node.find("icon")
                icon = icon_node.attrib.get("src", "") if icon_node is not None else ""
                channels.append({"id": cid, "name": display, "icon": icon, "source": source_name})
            _epg_source_status[url] = {
                "status": "ok", "channels": src_count,
                "fetched_at": time.time(), "error": None,
            }
        except Exception as exc:
            _epg_source_status[url] = {
                "status": "error", "channels": 0,
                "fetched_at": time.time(), "error": str(exc),
            }
            continue
    channels.sort(key=lambda c: c["name"].lower())
    return channels


def _get_epg_channels() -> list[dict]:
    """Return cached EPG channel list, rebuilding if stale."""
    global _epg_channel_cache, _epg_channel_cache_time
    if _epg_channel_cache and (time.time() - _epg_channel_cache_time) < _EPG_CHANNEL_CACHE_TTL:
        return _epg_channel_cache
    _epg_channel_cache = _build_epg_channel_list()
    _epg_channel_cache_time = time.time()
    return _epg_channel_cache


@app.get("/api/epg-sources/status")
def api_epg_source_status():
    """Return per-source fetch status (cached/pending/error)."""
    cache_age = time.time() - _epg_channel_cache_time if _epg_channel_cache_time else None
    cached = bool(_epg_channel_cache and cache_age is not None and cache_age < _EPG_CHANNEL_CACHE_TTL)

    # Gather all configured URLs
    all_urls = {}
    for src in get_active_sources():
        u = (src.get("epg_url") or "").strip()
        if u:
            all_urls[u] = src.get("name") or "Active Source"
    for es in list_epg_sources():
        if es.get("url"):
            all_urls[es["url"]] = es.get("name") or es["url"]

    statuses = {}
    for url, name in all_urls.items():
        info = _epg_source_status.get(url)
        if info:
            age = time.time() - info["fetched_at"]
            statuses[url] = {
                "status": info["status"],
                "channels": info["channels"],
                "ago_seconds": int(age),
                "error": info["error"],
            }
        else:
            statuses[url] = {"status": "pending", "channels": 0, "ago_seconds": None, "error": None}
    return {"cached": cached, "cache_age": int(cache_age) if cache_age else None, "sources": statuses}


@app.get("/api/epg-channels")
def api_epg_channels(q: str = Query(""), limit: int = Query(100, ge=1, le=1000)):
    """Browse/search available EPG channel entries from all configured EPG sources."""
    channels = _get_epg_channels()
    if q:
        q_lower = q.lower()
        channels = [c for c in channels if q_lower in c["name"].lower() or q_lower in c["id"].lower()]
    return {"channels": channels[:limit], "total": len(channels)}


@app.post("/api/epg-channels/refresh")
def api_refresh_epg_channels():
    """Force refresh the EPG channel cache."""
    global _epg_channel_cache, _epg_channel_cache_time
    _epg_channel_cache_time = 0
    channels = _get_epg_channels()
    return {"ok": True, "total": len(channels)}


def _similarity(a: str, b: str) -> float:
    """Simple token-overlap similarity score between 0 and 1."""
    a_tokens = set(re.sub(r"[^a-z0-9]+", " ", a.lower()).split())
    b_tokens = set(re.sub(r"[^a-z0-9]+", " ", b.lower()).split())
    # Remove very common filler words
    stop = {"the", "and", "or", "of", "tv", "hd", "sd", "us", "usa", "uk", "ca", "en", "channel"}
    a_tokens -= stop
    b_tokens -= stop
    if not a_tokens or not b_tokens:
        return 0.0
    overlap = a_tokens & b_tokens
    return len(overlap) / max(len(a_tokens), len(b_tokens))


@app.get("/api/epg-channels/auto-match")
def api_auto_match_preview(min_score: float = Query(0.4, ge=0.0, le=1.0),
                           limit: int = Query(500, ge=1, le=5000)):
    """Preview auto-match: find best EPG match for unmatched channels by similarity.
    Returns proposed matches without applying them."""
    epg_channels = _get_epg_channels()
    if not epg_channels:
        return {"matches": [], "unmatched": 0}

    asid = get_active_source_ids()
    all_channels = get_all_channels_raw(source_id=asid)

    # Find channels with no tvg_id or only dummy tvg_id
    unmatched = [ch for ch in all_channels
                 if not (ch.get("tvg_id") or "").strip()
                 or (ch.get("tvg_id") or "").startswith("dummy.")]

    # Build searchable EPG structures
    epg_names = [(ec["id"], ec["name"], ec.get("source", "")) for ec in epg_channels]
    epg_by_id = {ec["id"]: ec for ec in epg_channels}

    matches = []
    for ch in unmatched[:limit]:
        ch_name = ch.get("name") or ""
        # High-confidence shortcut: a known OTA diginet abbreviation
        # ("39.5 OAN" -> oneamericanews.us) maps straight to its guide.
        alias = _ota_diginet_alias(ch_name)
        if alias and alias in epg_by_id:
            ec = epg_by_id[alias]
            matches.append({
                "channel_id": ch["id"],
                "channel_name": ch_name,
                "epg_id": alias,
                "epg_name": ec.get("name", ""),
                "epg_source": ec.get("source", ""),
                "score": 1.0,
            })
            continue
        # Also try call-sign extraction for better matching
        cs = _extract_callsign(ch_name)
        best_id, best_name, best_score, best_source = "", "", 0.0, ""
        for eid, ename, esource in epg_names:
            # Score against full channel name
            score = _similarity(ch_name, ename)
            # Also score against EPG id prefix (e.g. "ESPN.us" -> "espn")
            id_prefix = eid.split(".")[0]
            id_score = _similarity(ch_name, id_prefix)
            score = max(score, id_score)
            # Score against cleaned EPG callsign (KDKADT1 -> KDKA)
            epg_cs = _extract_epg_callsign(eid)
            if epg_cs:
                epg_cs_score = _similarity(ch_name, epg_cs)
                score = max(score, epg_cs_score)
            # Extract callsign from EPG display name ("CBS (KDKA-DT1) Pittsburgh" -> "KDKA")
            dcs = _extract_display_callsign(ename)
            # If we extracted a call sign from the user channel, try that too
            if cs:
                cs_score = _similarity(cs, ename)
                cs_id_score = _similarity(cs, id_prefix)
                score = max(score, cs_score, cs_id_score)
                if epg_cs:
                    # Direct callsign-to-callsign comparison (exact match = 1.0)
                    if cs == epg_cs:
                        score = max(score, 1.0)
                    else:
                        score = max(score, _similarity(cs, epg_cs))
                if dcs:
                    if cs == dcs:
                        score = max(score, 1.0)
                    else:
                        score = max(score, _similarity(cs, dcs))
            if score > best_score:
                best_id, best_name, best_score, best_source = eid, ename, score, esource
        if best_score >= min_score:
            matches.append({
                "channel_id": ch["id"],
                "channel_name": ch_name,
                "epg_id": best_id,
                "epg_name": best_name,
                "epg_source": best_source,
                "score": round(best_score, 2),
            })

    matches.sort(key=lambda m: m["score"], reverse=True)
    return {"matches": matches, "unmatched": len(unmatched)}


@app.post("/api/epg-channels/auto-match")
def api_auto_match_apply(body: AutoMatchApply):
    """Apply selected auto-match results: set tvg_id on channels."""
    applied = 0
    for m in body.matches:
        cid = m.get("channel_id", "")
        epg_id = m.get("epg_id", "")
        if cid and epg_id:
            ch = get_channel(cid)
            if ch:
                update_channel(cid, tvg_id=epg_id)
                applied += 1
    if applied:
        schedule_export()
    return {"ok": True, "applied": applied}


# ── Export ────────────────────────────────────────────────────────

@app.post("/api/export")
def api_export():
    try:
        m3u, epg, count = write_exports()
    except requests.HTTPError as e:
        raise HTTPException(400, f"EPG download failed: {e}") from e
    # Invalidate guide cache so next load picks up new EPG
    _refresh_guide_cache()
    return {"m3u": str(m3u.relative_to(BASE_DIR)), "epg": str(epg.relative_to(BASE_DIR)),
            "channels": count}


# ── TV Guide ──────────────────────────────────────────────────────

_guide_cache: dict | None = None
_guide_cache_time: float = 0
_guide_cache_lock = threading.Lock()
_GUIDE_CACHE_TTL = 3600  # 1 hour

# Programmes are stored in the cache as tuples (not dicts) so 1-2M programme
# entries fit in ~100-200MB instead of ~600-800MB. These constants map index
# to field. See _compact_guide_payload for the read side.
_P_START, _P_STOP, _P_TITLE, _P_SUB, _P_EP, _P_DESC = range(6)

# <programme> child tags whose text we read in _build_guide_data. The streaming
# EPG parser must NOT el.clear() these on their own end event — that fires
# before the parent <programme> end event and would wipe the text.
_PROG_CHILD_TAGS = frozenset({"programme", "title", "sub-title", "desc", "episode-num"})


def _release_freed_memory():
    """Ask glibc to return free heap pages to the OS. Python's allocator
    doesn't do this on its own — once we've built (and freed) a multi-GB
    object like the EPG DOM, the process stays large until we call this.
    No-op on systems without glibc (mac/win)."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass

def _build_guide_data() -> dict:
    """Build guide data dict (heavy: parses EPG XML). Result is JSON-serializable."""
    asid = get_active_source_ids()
    groups = list_groups(source_id=asid)
    enabled_groups = [g for g in groups if g.get("enabled", True)]
    enabled_gids = {g["id"] for g in enabled_groups}

    # Build ordered group list for sidebar (respects editor sort order)
    group_order = {g["id"]: i for i, g in enumerate(enabled_groups)}
    gmap = {g["id"]: g.get("name", "") for g in enabled_groups}
    guide_groups = [{"id": g["id"], "name": g.get("name", ""), "count": 0} for g in enabled_groups]

    channels = get_all_channels_raw(source_id=asid)
    enabled = [ch for ch in channels if ch.get("enabled") and ch.get("group_id") in enabled_gids]

    # Keep guide channel-to-programme mapping consistent with exports.
    # Export can scope duplicate tvg-ids per source (e.g. "id__src5"), so
    # matching by raw DB tvg_id would miss guide rows that do exist in XML.
    selected_for_map = [(None, ch) for ch in enabled]
    export_tvg_map = _build_export_tvg_map(selected_for_map)

    # Sort by group order first, then channel sort_order within each group
    enabled.sort(key=lambda ch: (group_order.get(ch.get("group_id"), 9999), ch.get("sort_order", 0)))

    ch_list = []
    tvg_to_ch = {}
    for ch in enabled:
        tvg = (export_tvg_map.get(ch["id"]) or ch.get("tvg_id") or "").strip()
        entry = {
            "id": ch["id"],
            "name": ch.get("name") or "Unnamed",
            "logo": ch.get("logo") or "",
            "group": gmap.get(ch.get("group_id"), ""),
            "group_id": ch.get("group_id", ""),
            "tvg_id": tvg,
            "url": ch.get("url") or "",
            "programmes": [],
        }
        ch_list.append(entry)
        if tvg:
            tvg_to_ch.setdefault(tvg, []).append(entry)

    group_counts: dict[str, int] = {}
    for entry in ch_list:
        gid = entry.get("group_id") or ""
        group_counts[gid] = group_counts.get(gid, 0) + 1
    for group in guide_groups:
        group["count"] = group_counts.get(group["id"], 0)

    # The shared export snapshot already parsed every relevant programme.
    # Point guide channels at those compact tuples instead of parsing the
    # ~100 MB XMLTV file a second time.
    snapshot = _get_epg_snapshot()
    for tvg_id, channel_entries in tvg_to_ch.items():
        programmes = snapshot["programmes_by_id"].get(tvg_id, [])
        for channel_entry in channel_entries:
            fallback_name = channel_entry.get("name") or "Unknown Programme"
            channel_entry["programmes"] = [
                (p[0], p[1], p[2] or fallback_name, p[3], p[4],
                 p[5] or f"{fallback_name} — guide details unavailable")
                for p in programmes
            ]
    return {"channels": ch_list, "groups": guide_groups,
            "now": datetime.now(timezone.utc).isoformat()}

    epg_path = EXPORT_DIR / "organized.xml"
    if not epg_path.exists():
        try:
            write_exports()
        except Exception as exc:
            _record_backend_error("guide.build.write_exports_missing_xml", exc)

    if epg_path.exists():
        now = datetime.now(timezone.utc)
        try:
            _guide_days = max(1, min(14, int(get_setting("epg_window_days") or 4)))
        except (TypeError, ValueError):
            _guide_days = 4
        # The in-app guide shows a fixed 3-day span (still bounded by the
        # exported EPG window — can't show data that wasn't exported).
        _guide_days = min(_guide_days, 3)
        try:
            _guide_cache_max_programmes = max(48, min(400, int(get_setting("guide_cache_max_programmes") or 320)))
        except (TypeError, ValueError):
            _guide_cache_max_programmes = 320
        window_start = now - timedelta(hours=3)
        window_end = now + timedelta(days=_guide_days)
        fmt = "%Y%m%d%H%M%S %z"
        fmt_alt = "%Y%m%d%H%M%S"

        # Stream-parse the EPG so we never hold the full 100MB DOM in RAM.
        # iterparse builds a tree as it goes — we must clear() each finished
        # element AND drop it from root to actually free memory; just clearing
        # the child still leaves the root with a 52k-long children list.
        def _stream_parse_programmes(path):
            try:
                ctx = ET.iterparse(str(path), events=("start", "end"))
                it = iter(ctx)
                _, root = next(it)  # capture root so we can prune it
                for ev, el in it:
                    if ev == "end" and el.tag == "programme":
                        yield el
                        el.clear()
                        # Periodically drop processed programmes from root —
                        # otherwise root.children grows to 52k empty elements.
                        if len(root) > 256:
                            root.clear()
                    elif ev == "end" and el.tag not in _PROG_CHILD_TAGS:
                        # Drop <channel>/<display-name>/etc. to bound memory.
                        # Do NOT clear <programme> children here: their end
                        # events fire BEFORE the parent <programme> end event,
                        # so clearing them now wipes the .text we read below
                        # (every title fell back to the channel name — the
                        # whole guide showed channel names instead of titles).
                        el.clear()
            except (ET.ParseError, StopIteration):
                # Corrupt EPG — try regenerating once.
                try: write_exports()
                except Exception: return
                try:
                    ctx = ET.iterparse(str(path), events=("start", "end"))
                    it = iter(ctx)
                    _, root = next(it)
                    for ev, el in it:
                        if ev == "end" and el.tag == "programme":
                            yield el
                            el.clear()
                            if len(root) > 256: root.clear()
                        elif ev == "end" and el.tag not in _PROG_CHILD_TAGS:
                            el.clear()
                except Exception:
                    return

        for prog in _stream_parse_programmes(epg_path):
            ch_id = prog.attrib.get("channel", "")
            if ch_id not in tvg_to_ch:
                continue
            start_str = prog.attrib.get("start", "")
            stop_str = prog.attrib.get("stop", "")
            try:
                start = datetime.strptime(start_str, fmt)
            except ValueError:
                try: start = datetime.strptime(start_str[:14], fmt_alt).replace(tzinfo=timezone.utc)
                except ValueError: continue
            try:
                stop = datetime.strptime(stop_str, fmt)
            except ValueError:
                try: stop = datetime.strptime(stop_str[:14], fmt_alt).replace(tzinfo=timezone.utc)
                except ValueError: continue

            if stop < window_start or start > window_end:
                continue

            title_el = prog.find("title")
            sub_title_el = prog.find("sub-title")
            desc_el = prog.find("desc")
            raw_title = ((title_el.text or "") if title_el is not None else "").strip()
            raw_sub_title = ((sub_title_el.text or "") if sub_title_el is not None else "").strip()
            raw_ep_num = ""
            for epn in prog.findall("episode-num"):
                if (epn.attrib.get("system") or "").lower() == "onscreen":
                    raw_ep_num = (epn.text or "").strip()
                    if raw_ep_num: break
            raw_desc = ((desc_el.text or "") if desc_el is not None else "").strip()
            # Tuples are ~4× smaller than equivalent dicts in CPython. Layout
            # is fixed: (start, stop, title, sub_title, episode_num, desc) —
            # see _P_START..._P_DESC constants near _guide_cache.
            for ch_entry in tvg_to_ch[ch_id]:
                if len(ch_entry["programmes"]) >= _guide_cache_max_programmes:
                    continue
                ch_entry["programmes"].append((
                    start.isoformat(),
                    stop.isoformat(),
                    raw_title or ch_entry.get("name") or "Unknown Programme",
                    raw_sub_title,
                    raw_ep_num,
                    raw_desc or (f"{ch_entry.get('name') or 'Channel'} — guide details unavailable"),
                ))

    for ch in ch_list:
        ch["programmes"].sort(key=lambda p: p[_P_START])

    return {"channels": ch_list, "groups": guide_groups, "now": datetime.now(timezone.utc).isoformat()}


def _refresh_guide_cache():
    """Rebuild the guide cache (called from background thread or on-demand).
    Programmes are stored as 6-tuples (start, stop, title, sub_title,
    episode_num, desc) instead of dicts to cut per-entry memory ~3-4×."""
    global _guide_cache, _guide_cache_time
    with _guide_cache_lock:
        try:
            old = _guide_cache
            _guide_cache = _build_guide_data()
            _guide_cache_time = time.time()
            del old  # let the previous cache release ASAP before trim
            import gc
            gc.collect()
            _release_freed_memory()
        except Exception as exc:
            _record_backend_error("guide.cache.refresh", exc)


def _classify_guide_desc(desc: str) -> str:
    text = (desc or "").lower()
    if "no guide data" in text:
        return "no guide data"
    if "schedule inferred" in text or "guide details unavailable" in text:
        return "schedule inferred"
    return ""


def _compact_guide_payload(data: dict, max_programmes: int, include_full_desc: bool = False,
                           group_id: str | None = None) -> dict:
    """Shrink guide payload for fast UI loading while preserving current/next slots."""
    try:
        now = datetime.fromisoformat((data or {}).get("now") or datetime.now(timezone.utc).isoformat())
    except ValueError:
        now = datetime.now(timezone.utc)

    out_channels = []
    for ch in (data or {}).get("channels", []):
        if group_id and ch.get("group_id") != group_id:
            continue
        programmes = ch.get("programmes") or []
        keep = programmes
        if len(programmes) > max_programmes:
            idx = 0
            for i, p in enumerate(programmes):
                # Programmes are tuples now (see _P_* constants); fall back
                # to dict.get() if a legacy code path passed in dicts.
                stop_str = p[_P_STOP] if isinstance(p, tuple) else (p.get("stop") or "")
                try:
                    stop = datetime.fromisoformat(stop_str)
                except ValueError:
                    continue
                if stop >= now:
                    idx = i
                    break
            start = max(0, idx - 1)
            keep = programmes[start:start + max_programmes]

        slim_programmes = []
        for p in keep:
            if isinstance(p, tuple):
                p_start, p_stop, p_title, p_sub, p_ep, raw_desc = p
            else:
                p_start = p.get("start"); p_stop = p.get("stop")
                p_title = p.get("title") or ""; p_sub = p.get("sub_title") or ""
                p_ep = p.get("episode_num") or ""; raw_desc = p.get("desc") or ""
            slim_programmes.append({
                "start": p_start,
                "stop": p_stop,
                "title": p_title or "",
                "sub_title": p_sub or "",
                "episode_num": p_ep or "",
                "desc": (raw_desc[:280] if include_full_desc else _classify_guide_desc(raw_desc)),
            })

        out_channels.append({
            "id": ch.get("id"),
            "name": ch.get("name") or "Unnamed",
            "logo": ch.get("logo") or "",
            "group": ch.get("group") or "",
            "group_id": ch.get("group_id") or "",
            "tvg_id": ch.get("tvg_id") or "",
            "url": ch.get("url") or "",
            "programmes": slim_programmes,
        })

    return {
        "channels": out_channels,
        "groups": (data or {}).get("groups") or [],
        "now": (data or {}).get("now") or datetime.now(timezone.utc).isoformat(),
    }


def _current_group_listings(data: dict, group_id: str, now: datetime | None = None) -> dict:
    """Return only current/next programme metadata for one Editor group."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    listings = {}
    for channel in (data or {}).get("channels", []):
        if channel.get("group_id") != group_id:
            continue
        current = next_item = None
        for raw in channel.get("programmes") or []:
            if isinstance(raw, tuple):
                start_raw, stop_raw, title, sub_title, episode_num, desc = raw
            else:
                start_raw = raw.get("start"); stop_raw = raw.get("stop")
                title = raw.get("title") or ""; sub_title = raw.get("sub_title") or ""
                episode_num = raw.get("episode_num") or ""; desc = raw.get("desc") or ""
            try:
                start = datetime.fromisoformat(start_raw)
                stop = datetime.fromisoformat(stop_raw)
            except (TypeError, ValueError):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if stop.tzinfo is None:
                stop = stop.replace(tzinfo=timezone.utc)
            item = {
                "title": title or channel.get("name") or "Unknown Programme",
                "sub_title": sub_title or "", "episode_num": episode_num or "",
                "start": start_raw, "stop": stop_raw,
                "quality": _classify_guide_desc(desc),
            }
            if start <= now < stop:
                current = item
            elif start > now and next_item is None:
                next_item = item
            if current and next_item:
                break
        if current or next_item:
            listings[channel["id"]] = {"current": current, "next": next_item}
    return {"now": now.isoformat(), "listings": listings}


# ═══════════════════════════════════════════════════════════════════
# Smart Groups — EPG-based auto-populated groups
# ═══════════════════════════════════════════════════════════════════

def _get_smart_groups_config():
    """Return smart groups settings dict."""
    raw = get_setting("smart_groups")
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}

_GAME_INDICATOR_RE = re.compile(r'\bvs\.?\b|\bat\b|@', re.IGNORECASE)
# Stricter version for channel NAMES: avoids matching "AT" in "AT&T"
_NAME_GAME_RE = re.compile(r'\bvs\.?\s|\s@\s|\bat\b(?!&)', re.IGNORECASE)
# Source groups that indicate sports content
_SPORTS_GROUP_RE = re.compile(
    r'sport|nhl|mlb|nfl|nba|mls|espn|hockey|baseball|football|basketball'
    r'|soccer|ncaa|btn|big\s*ten|sportsnet|tnt|dazn|kayo|flo',
    re.IGNORECASE)
# League-specific source groups — used to detect which teams are playing.
# Only these channel types are trusted for game detection to avoid false
# positives from analysis shows, draft previews, etc. on generic networks.
_LEAGUE_DETECT_RE = re.compile(
    r'\bNHL\b|\bMLB\b|\bNFL\b|\bNBA\b|\bMLS\b|\bNCAA\b|\bWNBA\b|\bCHL\b'
    r'|\bTeams?\b|\bSportsnet\+',
    re.IGNORECASE)
# Extract numbered sport-channel slots for dedup: "NHL 08", "MLB 05", etc.
_SLOT_RE = re.compile(
    r'(NHL|MLB|NFL|NBA|MLS|CHL|NCAA\s*\w+|ESPN\s*\+?|ESPN\s*PLUS|SPORTSNET\s*\+?)'
    r'\s*[|:\-]?\s*(\d{2,3})\b',
    re.IGNORECASE)
_EVENT_MONTH_DAY_RE = re.compile(
    r'\((\d{1,2})\.(\d{1,2})\s+(\d{1,2}(?::\d{2})?\s*[AP]M)\s+([A-Z]{2})(?:/\d{1,2}(?::\d{2})?\s*[AP]M\s+[A-Z]{2})?\)',
    re.IGNORECASE)
_EVENT_ISO_RE = re.compile(
    r'\((\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?\)',
    re.IGNORECASE)
_EVENT_AT_DATE_RE = re.compile(
    r'@\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{1,2}(?::\d{2})?\s*[AP]M)\s+([A-Z]{2})',
    re.IGNORECASE)
_EVENT_DAY_MONTH_RE = re.compile(
    r'\b(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{1,2}(?::\d{2})?\s*[AP]M)\s+([A-Z]{2})\b',
    re.IGNORECASE)

_NHL_TEAM_ABBREVS = {
    "Ducks": "ANA",
    "Bruins": "BOS",
    "Sabres": "BUF",
    "Flames": "CGY",
    "Hurricanes": "CAR",
    "Blackhawks": "CHI",
    "Avalanche": "COL",
    "Blue Jackets": "CBJ",
    "Stars": "DAL",
    "Red Wings": "DET",
    "Oilers": "EDM",
    "Panthers": "FLA",
    "Kings": "LAK",
    "Wild": "MIN",
    "Canadiens": "MTL",
    "Predators": "NSH",
    "Devils": "NJD",
    "Islanders": "NYI",
    "Rangers": "NYR",
    "Senators": "OTT",
    "Flyers": "PHI",
    "Penguins": "PIT",
    "Kraken": "SEA",
    "Sharks": "SJS",
    "Blues": "STL",
    "Lightning": "TBL",
    "Maple Leafs": "TOR",
    "Canucks": "VAN",
    "Golden Knights": "VGK",
    "Capitals": "WSH",
    "Jets": "WPG",
    "Mammoth": "UTA",
    "Utah": "UTA",
}


def _extract_slot(ch_name):
    """Extract sport-channel slot like 'nhl-8' for dedup across sources."""
    m = _SLOT_RE.search(ch_name)
    if not m:
        return None
    prefix = re.sub(r'[^a-z+]+', '', m.group(1).lower()).replace('plus', '+')
    num = m.group(2).lstrip('0') or '0'
    return f"{prefix}-{num}"


def _parse_channel_event_start(ch_name, now):
    """Parse scheduled event start from sports channel names.

    Supported examples:
    - NHL 01: Flyers vs. Penguins (4.20 7:30 PM ET)
    - Round 1 Game 2: Flyers @ Penguins (2026-04-21 00:00:00)
    - ESPN+ 11 : Nevada vs. Northern Colorado @ Apr 20 04:00 PM ET
    - MLB 01 | Padres at Pirates HOME 08 Apr 12:35 PM ET
    """
    text = ch_name or ""

    def _parse_local(year, month, day, time_text, tz_code):
        offsets = {
            "ET": -4,
            "CT": -5,
            "MT": -6,
            "PT": -7,
        }
        norm_time = time_text.upper().replace(" ", "")
        if ":" not in norm_time:
            norm_time = norm_time.replace("AM", ":00AM").replace("PM", ":00PM")
        try:
            local_start = datetime.strptime(f"{year}-{month:02d}-{day:02d} {norm_time}", "%Y-%m-%d %I:%M%p")
        except ValueError:
            return None
        offset = offsets.get((tz_code or "").upper())
        if offset is not None:
            local_start = local_start.replace(tzinfo=timezone(timedelta(hours=offset)))
            return local_start.astimezone(timezone.utc)
        return local_start.replace(tzinfo=timezone.utc)

    m = _EVENT_MONTH_DAY_RE.search(text)
    if m:
        month = int(m.group(1))
        day = int(m.group(2))
        parsed = _parse_local(now.year, month, day, m.group(3), m.group(4))
        if parsed is not None:
            return parsed

    m = _EVENT_ISO_RE.search(text)
    if m:
        try:
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            hour = int(m.group(4))
            minute = int(m.group(5))
            second = int(m.group(6) or 0)
            return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except ValueError:
            pass

    m = _EVENT_AT_DATE_RE.search(text)
    if m:
        try:
            month = datetime.strptime(m.group(1), "%b").month
        except ValueError:
            month = now.month
        parsed = _parse_local(now.year, month, int(m.group(2)), m.group(3), m.group(4))
        if parsed is not None:
            return parsed

    m = _EVENT_DAY_MONTH_RE.search(text)
    if m:
        try:
            month = datetime.strptime(m.group(2), "%b").month
        except ValueError:
            month = now.month
        parsed = _parse_local(now.year, month, int(m.group(1)), m.group(3), m.group(4))
        if parsed is not None:
            return parsed

    return None


def _channel_event_in_window(ch_name, now, window_start, window_end):
    """Return True when a channel name encodes a game time within the smart-group window."""
    start = _parse_channel_event_start(ch_name, now)
    if start is None:
        return False
    return window_start <= start <= window_end


@lru_cache(maxsize=128)
def _fetch_nhl_month_schedule(team_abbrev, year_month):
    """Fetch official NHL monthly schedule for a club abbreviation."""
    url = f"https://api-web.nhle.com/v1/club-schedule/{team_abbrev}/month/{year_month}"
    r = requests.get(url, timeout=20, headers={"User-Agent": "M3UBoss/2.0"})
    r.raise_for_status()
    data = r.json()
    return data.get("games", []) if isinstance(data, dict) else []


def _iter_month_keys(window_start, window_end):
    keys = []
    year = window_start.year
    month = window_start.month
    while (year, month) <= (window_end.year, window_end.month):
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            month = 1
            year += 1
    return keys


def _fetch_nhl_games_in_window(team_abbrev, window_start, window_end):
    games = []
    for month_key in _iter_month_keys(window_start, window_end):
        try:
            month_games = _fetch_nhl_month_schedule(team_abbrev, month_key)
        except Exception:
            continue
        for game in month_games:
            start_text = game.get("startTimeUTC") or ""
            if not start_text:
                continue
            try:
                start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if window_start <= start <= window_end:
                games.append(game)
    return games


# Channels to exclude from smart city groups: radio and local stations
_RADIO_RE = re.compile(r'\bradio\b', re.IGNORECASE)
_LOCAL_RE = re.compile(r'\bLocal\b', re.IGNORECASE)

def _is_excluded_smart_channel(ch):
    """Return True if channel should be excluded from smart city groups."""
    name = (ch.get("name") or "").lower()
    sg = (ch.get("source_group") or "").lower()
    if _RADIO_RE.search(name) or _RADIO_RE.search(sg):
        return True
    if _LOCAL_RE.search(sg):
        return True
    return False


def _smart_channel_priority(ch, city_lower):
    """Return sort key tuple: lower = higher priority in the smart group."""
    name = (ch.get("name") or "").lower()
    sg = (ch.get("source_group") or "").lower()
    is_radio = "radio" in name or "radio" in sg
    has_city = city_lower in name

    if is_radio:
        return (90, name)
    # Dedicated team channels (Pittsburgh Penguins, Pittsburgh Pirates, etc.)
    if has_city and ("team" in sg):
        return (0, name)
    # Regional sports network (AT&T SportsNet Pittsburgh, etc.)
    if has_city and ("sport" in sg or "sport" in name):
        return (5, name)
    # Other city-named channels
    if has_city:
        return (10, name)
    # Major league game broadcasts (NHL, MLB, NFL, NBA, MLS)
    major_leagues = ("nhl", "mlb", "nfl", "nba", "mls")
    if any(m in sg for m in major_leagues):
        return (20, name)
    # Major US broadcast networks (ESPN, TNT, FOX, etc.)
    major_nets = ("espn", "tnt", "tbs", "fox", "hbo", "max", "abc", "nbc", "cbs")
    if any(m in sg for m in major_nets):
        return (25, name)
    # Other sports
    if "sport" in sg:
        return (30, name)
    return (50, name)


# ── Locals smart group ───────────────────────────────────────────
# A separate sub-feature of smart groups: populates the existing
# "US | Locals" group with shadow rows of local-station channels for
# the user's selected cities. Uses a dedicated smart_group_id so deletes
# never touch real channels the user has placed in the group manually.

_LOCALS_GROUP_NAME = "US | Locals"
_LOCALS_SMART_ID = "smart_locals_us"
# Source-group prefixes that hold the per-station local channels.
_LOCALS_NETWORK_PREFIX = "US | Local"
# Weather channels to optionally prepend. Match by canonical name; the
# duplicates from different sources are deduped by URL later.
_LOCALS_WEATHER_NAMES = ("the weather channel", "accuweather", "fox weather")
# Channel-name parser: "PA | Pittsburgh | NBC 11 WPXI" -> ("PA", "Pittsburgh", rest)
_LOCALS_NAME_RE = re.compile(r'^\s*([A-Za-z]{2})\s*\|\s*([^|]+?)\s*\|\s*(.*)$')


def _parse_locals_city_entry(entry: str):
    """Parse a user-entered city like 'Pittsburgh, PA' or 'Pittsburgh'.
    Returns (city_lower, state_upper_or_None) or None if blank.
    """
    s = (entry or "").strip()
    if not s:
        return None
    if "," in s:
        city, state = s.rsplit(",", 1)
        state = state.strip().upper()
        if len(state) != 2 or not state.isalpha():
            state = None
    else:
        city, state = s, None
    city = city.strip().lower()
    if not city:
        return None
    return (city, state)


def _refresh_locals_smart_group(real_channels, log):
    """Populate the 'US | Locals' group with shadows for selected cities.

    real_channels: list of non-shadow channel dicts (already loaded by caller).
    Always deletes the prior locals shadows first. If the feature is
    disabled or the target group is missing, simply leaves it empty.
    """
    delete_smart_group_channels(_LOCALS_SMART_ID)

    config = _get_smart_groups_config() or {}
    if not config.get("locals_enabled"):
        return

    raw_entries = config.get("locals_cities") or []
    parsed = [p for p in (_parse_locals_city_entry(e) for e in raw_entries) if p]
    if not parsed:
        log.info("Locals smart group: enabled but no cities configured")
        return

    target = find_group_by_name(_LOCALS_GROUP_NAME)
    if not target:
        log.warning("Locals smart group: target group %r not found — skipping",
                    _LOCALS_GROUP_NAME)
        return

    # Index cities for lookup: city_lower -> set of states (None = any)
    by_city: dict[str, set] = {}
    for city, state in parsed:
        by_city.setdefault(city, set()).add(state)

    include_weather = bool(config.get("locals_include_weather", True))

    matched_local: list[dict] = []
    matched_weather: list[dict] = []

    for ch in real_channels:
        if ch.get("smart_group_id"):
            continue
        if not ch.get("enabled", 1):
            continue
        name = (ch.get("name") or "").strip()
        if not name:
            continue
        sg = (ch.get("source_group") or "")

        # Weather: match by canonical name (deduped later by URL)
        if include_weather and name.lower() in _LOCALS_WEATHER_NAMES:
            matched_weather.append(ch)
            continue

        # Locals: must be from a "US | Local *" source group, and the
        # name must parse to a known city (and matching state if given).
        if not sg.startswith(_LOCALS_NETWORK_PREFIX):
            continue
        m = _LOCALS_NAME_RE.match(name)
        if not m:
            continue
        ch_state = m.group(1).upper()
        ch_city = m.group(2).strip().lower()
        states = by_city.get(ch_city)
        if states is None:
            continue
        # None in states = match-any-state for that city entry
        if None not in states and ch_state not in states:
            continue
        matched_local.append(ch)

    # Dedup by URL (sources duplicate the same station 3-4x). Weather
    # entries sort by their canonical order (Weather Channel first, etc.).
    def _weather_sort_key(c):
        nm = (c.get("name") or "").lower()
        try:
            return (_LOCALS_WEATHER_NAMES.index(nm), nm)
        except ValueError:
            return (99, nm)

    def _local_sort_key(c):
        # Order: by user's city entry order, then channel name.
        nm = (c.get("name") or "")
        m = _LOCALS_NAME_RE.match(nm)
        if not m:
            return (99, 99, nm.lower())
        ch_city = m.group(2).strip().lower()
        ch_state = m.group(1).upper()
        # Find earliest matching user entry index
        idx = 99
        for i, (city, state) in enumerate(parsed):
            if city != ch_city:
                continue
            if state is not None and state != ch_state:
                continue
            idx = min(idx, i)
        return (idx, 0, nm.lower())

    matched_weather.sort(key=_weather_sort_key)
    matched_local.sort(key=_local_sort_key)

    # Weather: collapse multiple sources of the same canonical channel to one.
    seen_weather_name: set[str] = set()
    weather_unique: list[dict] = []
    for ch in matched_weather:
        nm = (ch.get("name") or "").lower()
        if nm in seen_weather_name:
            continue
        seen_weather_name.add(nm)
        weather_unique.append(ch)

    ordered = weather_unique + matched_local
    seen_urls: set[str] = set()
    seen_tvg: set[str] = set()
    deduped: list[dict] = []
    for ch in ordered:
        url = ch.get("url", "")
        if not url or url in seen_urls:
            continue
        tvg = (ch.get("tvg_id") or "").strip().lower()
        if tvg and not tvg.startswith("dummy") and tvg in seen_tvg:
            continue
        seen_urls.add(url)
        if tvg and not tvg.startswith("dummy"):
            seen_tvg.add(tvg)
        deduped.append(ch)

    if not deduped:
        log.info("Locals smart group: 0 channels matched (cities=%s, weather=%s)",
                 raw_entries, include_weather)
        return

    shadows = []
    for i, ch in enumerate(deduped):
        shadows.append({
            "id": new_id(),
            "source_id": ch["source_id"],
            "stream_id": ch.get("stream_id"),
            "name": ch.get("name", "Unnamed"),
            "source_name": ch.get("source_name", ""),
            "source_group": ch.get("source_group", ""),
            "tvg_id": ch.get("tvg_id", ""),
            "original_tvg_id": ch.get("original_tvg_id", ""),
            "tvg_name": ch.get("tvg_name", ""),
            "logo": ch.get("logo", ""),
            "url": ch.get("url", ""),
            "channel_number": ch.get("channel_number", ""),
            "sort_order": i,
        })
    insert_smart_group_channels(shadows, _LOCALS_SMART_ID, target["id"])
    n_weather_kept = sum(1 for c in deduped if (c.get("name") or "").lower() in _LOCALS_WEATHER_NAMES)
    log.info("Locals smart group: %d channels (%d weather, %d locals)",
             len(shadows), n_weather_kept, len(shadows) - n_weather_kept)


def _refresh_smart_groups():
    """Scan channel names and EPG programme titles to populate smart groups.

    Matching strategies (designed to avoid false positives):

    **Strategy 1 — Permanent channels (by city+team name):**
    Channel name contains a full "City Team" phrase (e.g. "Pittsburgh
    Penguins") OR contains the city name AND is in a sports source group.
    Channels whose names embed game schedule data (contain vs/@/at) are
    SKIPPED — those are rotating channels with potentially stale names.

    **Strategy 2 — Live game broadcasts (by EPG programme title):**
    An EPG programme title within the lookahead window contains a team
    keyword AND a game indicator (vs/@/at).  Only channels in sports
    source groups are eligible.  Opponent team channels (whose name
    matches another city's team) are excluded.  Dummy EPG channels are
    skipped.

    **Strategy 3 — Live game broadcasts (by channel name fallback):**
    If the provider updates channel names before EPG titles, timed event
    channels like "NHL 01: Flyers vs. Penguins (4.20 7:30 PM ET)" are
    included when their encoded start time falls within the smart-group
    window.

    **Deduplication (3 layers):**
    1. Exact URL match
    2. Same tvg_id (case-insensitive, ignoring dummy IDs)
    3. Same sport-channel slot (e.g. "NHL 08" from two sources → keep one)

    Channels are sorted by priority before dedup, so the best version
    (team channel > league broadcast > network > radio) is always kept.
    """
    import logging
    log = logging.getLogger("m3u_boss.smart_groups")

    config = _get_smart_groups_config()
    if not config or not config.get("enabled"):
        delete_all_smart_group_channels()
        return

    cities = config.get("cities", [])
    window_hours = config.get("window_hours", 4)

    # We stream-parse the EPG below instead of loading the whole 111MB DOM —
    # see _build_guide_data for the same pattern. Saves ~1-1.5GB of memory
    # every time smart groups refresh.
    epg_path = EXPORT_DIR / "organized.xml"
    epg_available = epg_path.exists()

    now = datetime.now(timezone.utc)
    asid = get_active_source_ids()
    all_channels = get_all_channels_raw(source_id=asid)
    real_channels = [ch for ch in all_channels if not ch.get("smart_group_id")]

    # Build tvg_id → channel(s) lookup
    tvg_to_channels = {}
    for ch in real_channels:
        tvg = (ch.get("tvg_id") or "").strip()
        if tvg:
            tvg_to_channels.setdefault(tvg, []).append(ch)

    # Pre-parse EPG programme titles in the time window
    # Skip dummy EPG entries — their titles are channel names with stale data
    fmt = "%Y%m%d%H%M%S %z"
    fmt_alt = "%Y%m%d%H%M%S"
    window_start = now - timedelta(hours=1)
    window_end = now + timedelta(hours=window_hours)

    tvg_programme_titles: dict[str, set[str]] = {}
    if epg_available:
        try:
            ctx = ET.iterparse(str(epg_path), events=("start", "end"))
            it = iter(ctx)
            _, _root = next(it)
            for ev, prog in it:
                if ev != "end" or prog.tag != "programme":
                    if ev == "end": prog.clear()
                    continue
                ch_id = prog.attrib.get("channel", "")
                if not ch_id or ch_id not in tvg_to_channels or ch_id.startswith("dummy"):
                    prog.clear()
                    if len(_root) > 256: _root.clear()
                    continue
                start_str = prog.attrib.get("start", "")
                stop_str = prog.attrib.get("stop", "")
                try:
                    start = datetime.strptime(start_str, fmt)
                except ValueError:
                    try: start = datetime.strptime(start_str[:14], fmt_alt).replace(tzinfo=timezone.utc)
                    except ValueError: prog.clear(); continue
                try:
                    stop = datetime.strptime(stop_str, fmt)
                except ValueError:
                    try: stop = datetime.strptime(stop_str[:14], fmt_alt).replace(tzinfo=timezone.utc)
                    except ValueError: prog.clear(); continue

                if stop < window_start or start > window_end:
                    prog.clear()
                    if len(_root) > 256: _root.clear()
                    continue

                title_el = prog.find("title")
                title = ((title_el.text or "") if title_el is not None else "")
                if title:
                    tvg_programme_titles.setdefault(ch_id, set()).add(title)
                prog.clear()
                if len(_root) > 256: _root.clear()
        except Exception as e:
            log.warning("Smart groups: failed to parse EPG XML: %s", e)

    # Build a set of ALL known team names across all cities for cross-validation.
    # When detecting if a team is "playing", we verify the opponent is also a
    # recognized team — filters out false positives like "Kobe Steelers" (rugby).
    _all_known_teams_set: set[str] = set()
    _all_known_team_pats: list[re.Pattern] = []
    for _cd in SPORTS_CITIES.values():
        for _t in _cd["teams"]:
            tl = _t.lower()
            if tl not in _all_known_teams_set:
                _all_known_teams_set.add(tl)
                try:
                    _all_known_team_pats.append(re.compile(r'\b' + re.escape(tl) + r'\b', re.IGNORECASE))
                except re.error:
                    pass

    def _count_known_teams_in(text):
        """Count how many distinct known team names appear in text."""
        return sum(1 for p in _all_known_team_pats if p.search(text))

    # Process each city
    sg_base_id = "smart_city_"
    selected_set = set(cities)
    custom_kw = [k.lower().strip() for k in config.get("custom_keywords", []) if k.strip()]

    for city, city_data in SPORTS_CITIES.items():
        sg_id = sg_base_id + city.lower().replace(" ", "_")
        sg_name = f"{city} Sports"

        # Always delete old shadows first
        delete_smart_group_channels(sg_id)

        group = find_group_by_name(sg_name)

        if city not in selected_set:
            if group:
                update_group(group["id"], enabled=0)
            continue

        # ── Build patterns for this city ──────────────────────────
        team_names = [t for t in city_data["teams"]]
        all_kw_raw = [t.lower() for t in team_names] + custom_kw

        # Team keyword patterns (bare team name, word-boundary)
        team_patterns = []
        for kw in all_kw_raw:
            try:
                team_patterns.append(re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE))
            except re.error:
                continue
        if not team_patterns:
            continue

        # Full "City Team" phrase patterns for precise channel-name matching
        full_team_patterns = []
        for t in team_names:
            try:
                full_team_patterns.append(
                    re.compile(r'\b' + re.escape(city) + r'\s+' + re.escape(t) + r'\b', re.IGNORECASE))
            except re.error:
                continue

        city_pattern = re.compile(r'\b' + re.escape(city) + r'\b', re.IGNORECASE)

        def _has_team(text):
            return any(p.search(text) for p in team_patterns)

        def _is_game_with_team(text):
            return _GAME_INDICATOR_RE.search(text) and _has_team(text)

        def _has_full_team(text):
            return any(p.search(text) for p in full_team_patterns)

        official_opponents_by_idx = {}
        for idx, team_name in enumerate(team_names):
            team_abbrev = _NHL_TEAM_ABBREVS.get(team_name)
            if not team_abbrev:
                continue
            for game in _fetch_nhl_games_in_window(team_abbrev, window_start, window_end):
                away = game.get("awayTeam") or {}
                home = game.get("homeTeam") or {}
                away_abbrev = (away.get("abbrev") or "").upper()
                home_abbrev = (home.get("abbrev") or "").upper()
                is_away = away_abbrev == team_abbrev
                is_home = home_abbrev == team_abbrev
                if not (is_away or is_home):
                    continue
                opponent = home if is_away else away
                tokens = set()
                common = ((opponent.get("commonName") or {}).get("default") or "").strip()
                place = ((opponent.get("placeName") or {}).get("default") or "").strip()
                if common:
                    tokens.add(common.lower())
                if place:
                    tokens.add(place.lower())
                if tokens:
                    official_opponents_by_idx.setdefault(idx, set()).update(tokens)

        def _matches_official_nhl_schedule(text):
            lower = (text or "").lower()
            for idx, opponents in official_opponents_by_idx.items():
                if idx >= len(team_patterns):
                    continue
                if not team_patterns[idx].search(lower):
                    continue
                if any(re.search(r'\b' + re.escape(token) + r'\b', lower, re.IGNORECASE) for token in opponents):
                    return idx
            return None

        # ── Determine which teams actually have a game in the window ──
        # Only trust league-specific channels (NHL, MLB, NFL, NBA, NCAA,
        # Team channels) — NOT generic sports networks like ESPN where
        # draft previews and analysis shows mention teams in non-game context.
        teams_playing = set()  # indices into team_names/team_patterns
        for tvg_id, titles in tvg_programme_titles.items():
            # Check if this channel belongs to a league source group
            chs_for_tvg = tvg_to_channels.get(tvg_id, [])
            sg_combined = " ".join((c.get("source_group") or "") for c in chs_for_tvg)
            if not _LEAGUE_DETECT_RE.search(sg_combined):
                continue
            for title in titles:
                if not _GAME_INDICATOR_RE.search(title):
                    continue
                for idx, pat in enumerate(team_patterns):
                    if idx >= len(team_names):
                        continue
                    if not pat.search(title):
                        continue
                    # Cross-validate: title has city name, or 2+ known teams
                    if city_pattern.search(title):
                        teams_playing.add(idx)
                    elif _count_known_teams_in(title) >= 2:
                        teams_playing.add(idx)

        for idx in official_opponents_by_idx:
            teams_playing.add(idx)

        # Fallback: infer playing teams from timed event channel names when
        # the provider updates the channel name before the EPG title.
        for ch in real_channels:
            if _is_excluded_smart_channel(ch):
                continue
            sg = (ch.get("source_group") or "")
            if not _LEAGUE_DETECT_RE.search(sg):
                continue
            ch_name = ch.get("name") or ""
            if not _GAME_INDICATOR_RE.search(ch_name):
                continue
            official_idx = _matches_official_nhl_schedule(ch_name)
            if official_idx is not None:
                teams_playing.add(official_idx)
                continue
            if not _channel_event_in_window(ch_name, now, window_start, window_end):
                continue
            if not (city_pattern.search(ch_name) or _count_known_teams_in(ch_name) >= 2):
                continue
            for idx, pat in enumerate(team_patterns):
                if idx >= len(team_names):
                    continue
                if pat.search(ch_name):
                    teams_playing.add(idx)

        if not teams_playing:
            # No games in the window — still populate with team channels only
            if not group:
                group = create_group(sg_name, is_custom=True)
            update_group(group["id"], pinned=1, sort_order=-1, enabled=1)
            # Collect team channels even with no games scheduled
            candidates = []
            for ch in real_channels:
                if _is_excluded_smart_channel(ch):
                    continue
                ch_name = ch.get("name") or ""
                sg_val = (ch.get("source_group") or "")
                if "team" in sg_val.lower() and _has_full_team(ch_name):
                    candidates.append(ch)
            if candidates:
                candidates.sort(key=lambda ch: _smart_channel_priority(ch, city.lower()))
                # Dedup by URL
                seen = set()
                deduped = []
                for ch in candidates:
                    url = ch.get("url", "")
                    if url and url not in seen:
                        seen.add(url)
                        deduped.append(ch)
                if deduped:
                    shadows = []
                    for i, ch in enumerate(deduped):
                        shadows.append({
                            "id": new_id(),
                            "source_id": ch["source_id"],
                            "stream_id": ch.get("stream_id"),
                            "name": ch.get("name", "Unnamed"),
                            "source_name": ch.get("source_name", ""),
                            "source_group": ch.get("source_group", ""),
                            "tvg_id": ch.get("tvg_id", ""),
                            "original_tvg_id": ch.get("original_tvg_id", ""),
                            "tvg_name": ch.get("tvg_name", ""),
                            "logo": ch.get("logo", ""),
                            "url": ch.get("url", ""),
                            "channel_number": ch.get("channel_number", ""),
                            "sort_order": i,
                        })
                    insert_smart_group_channels(shadows, sg_id, group["id"])
            log.info("Smart group '%s': no games in next %dh, %d team channels", sg_name, window_hours, len(candidates))
            continue

        # Build patterns for ONLY the teams that are playing
        playing_team_patterns = [team_patterns[i] for i in teams_playing]
        playing_full_patterns = [full_team_patterns[i] for i in teams_playing if i < len(full_team_patterns)]
        playing_team_names_lower = {team_names[i].lower() for i in teams_playing if i < len(team_names)}

        def _has_playing_team(text):
            return any(p.search(text) for p in playing_team_patterns)

        def _has_playing_full_team(text):
            return any(p.search(text) for p in playing_full_patterns)

        def _is_game_with_playing_team(text):
            return _GAME_INDICATOR_RE.search(text) and _has_playing_team(text)

        # ── Collect candidates (before dedup) ─────────────────────
        candidates = []

        # Strategy 0: ALWAYS include dedicated team channels (from Teams groups)
        # regardless of whether a game is in the EPG window.
        # These are permanent channels like "Pittsburgh Penguins" in
        # "Sports | NHL Teams".
        for ch in real_channels:
            if _is_excluded_smart_channel(ch):
                continue
            ch_name = ch.get("name") or ""
            sg = (ch.get("source_group") or "")
            if "team" in sg.lower() and _has_full_team(ch_name):
                candidates.append(ch)

        # Strategy 1: Permanent city channels — only for teams that are playing
        # Must have city name + full city-team phrase for a playing team,
        # or city name + sports source group (regional sports network).
        # Skip names with game indicators (rotating channels with stale names).
        # Exclude radio and local station channels.
        for ch in real_channels:
            if _is_excluded_smart_channel(ch):
                continue
            ch_name = ch.get("name") or ""
            if not city_pattern.search(ch_name):
                continue
            if _NAME_GAME_RE.search(ch_name):
                continue
            sg = (ch.get("source_group") or "")
            # Full team phrase match — but only for playing teams
            if _has_playing_full_team(ch_name):
                candidates.append(ch)
            # Regional sports networks (e.g. AT&T SportsNet Pittsburgh)
            elif _SPORTS_GROUP_RE.search(sg) and "team" not in sg.lower():
                candidates.append(ch)

        # Strategy 2: Live game broadcasts via EPG — only playing teams
        # Only include channels from league-specific source groups (NHL, MLB,
        # NFL, NBA, etc.) to avoid pulling in random regional/international
        # broadcasts (Sportsnet East, Sky Sport Mix, etc.) that happen to air
        # the same game.
        # Cross-validate each match: EPG title must have city name or 2+
        # known teams to avoid false positives from ambiguous names
        # (e.g. "East Carolina Pirates" matching "Pirates").
        # Exclude radio and local station channels.
        for tvg_id, titles in tvg_programme_titles.items():
            matching_titles = [t for t in titles if _is_game_with_playing_team(t)]
            if not matching_titles:
                continue
            # At least one matching title must pass cross-validation
            if not any(
                city_pattern.search(t) or _count_known_teams_in(t) >= 2
                for t in matching_titles
            ):
                continue
            for ch in tvg_to_channels.get(tvg_id, []):
                if _is_excluded_smart_channel(ch):
                    continue
                sg = (ch.get("source_group") or "")
                if not _LEAGUE_DETECT_RE.search(sg):
                    continue
                if "team" in sg.lower():
                    ch_name = ch.get("name") or ""
                    if not _has_playing_full_team(ch_name) and not city_pattern.search(ch_name):
                        continue
                candidates.append(ch)

        # Strategy 3: Timed live-event channels via channel name fallback.
        # This catches league slots like NHL 01 when the channel name has the
        # correct matchup but the provider has not populated the EPG title yet.
        for ch in real_channels:
            if _is_excluded_smart_channel(ch):
                continue
            sg = (ch.get("source_group") or "")
            if not _LEAGUE_DETECT_RE.search(sg):
                continue
            ch_name = ch.get("name") or ""
            if not _is_game_with_playing_team(ch_name):
                official_idx = _matches_official_nhl_schedule(ch_name)
                if official_idx is None:
                    continue
            elif not _channel_event_in_window(ch_name, now, window_start, window_end):
                official_idx = _matches_official_nhl_schedule(ch_name)
                if official_idx is None:
                    continue
            if not (city_pattern.search(ch_name) or _count_known_teams_in(ch_name) >= 2):
                continue
            candidates.append(ch)

        # ── Sort by priority, then dedup ──────────────────────────
        candidates.sort(key=lambda ch: _smart_channel_priority(ch, city.lower()))

        matched_channels = []
        seen_urls = set()
        seen_tvg = set()
        seen_slots = set()

        for ch in candidates:
            url = ch.get("url", "")
            if not url or url in seen_urls:
                continue
            # Dedup by tvg_id (case-insensitive, skip dummy IDs)
            tvg = (ch.get("tvg_id") or "").strip().lower()
            if tvg and not tvg.startswith("dummy") and tvg in seen_tvg:
                continue
            # Dedup by sport-channel slot (e.g. "NHL 08" from two sources)
            slot = _extract_slot(ch.get("name") or "")
            if slot and slot in seen_slots:
                continue
            seen_urls.add(url)
            if tvg and not tvg.startswith("dummy"):
                seen_tvg.add(tvg)
            if slot:
                seen_slots.add(slot)
            matched_channels.append(ch)

        # ── Create group and shadows ──────────────────────────────
        if not group:
            group = create_group(sg_name, is_custom=True)
        update_group(group["id"], pinned=1, sort_order=-1, enabled=1)

        if matched_channels:
            shadows = []
            for i, ch in enumerate(matched_channels):
                shadows.append({
                    "id": new_id(),
                    "source_id": ch["source_id"],
                    "stream_id": ch.get("stream_id"),
                    "name": ch.get("name", "Unnamed"),
                    "source_name": ch.get("source_name", ""),
                    "source_group": ch.get("source_group", ""),
                    "tvg_id": ch.get("tvg_id", ""),
                    "original_tvg_id": ch.get("original_tvg_id", ""),
                    "tvg_name": ch.get("tvg_name", ""),
                    "logo": ch.get("logo", ""),
                    "url": ch.get("url", ""),
                    "channel_number": ch.get("channel_number", ""),
                    "sort_order": i,
                })
            insert_smart_group_channels(shadows, sg_id, group["id"])
            log.info("Smart group '%s': %d channels matched", sg_name, len(shadows))
        else:
            log.info("Smart group '%s': no matching channels/programmes in next %dh", sg_name, window_hours)

    # Refresh the locals smart group (independent of the sports section).
    try:
        _refresh_locals_smart_group(real_channels, log)
    except Exception as exc:
        log.warning("Locals smart group refresh failed: %s", exc)

    # Re-export since group contents changed
    schedule_export(delay=1.0)


def _promote_city_channels(config, log):
    """Move channels matching selected cities' teams to the top of their
    Teams groups (by team name) and league groups (by game/event name)."""
    cities = config.get("cities", [])
    if not cities:
        return

    # Collect all team keywords for selected cities
    team_keywords = []
    for city in cities:
        city_data = SPORTS_CITIES.get(city)
        if not city_data:
            continue
        for team in city_data["teams"]:
            team_keywords.append(team.lower())
        # Also add the city name itself for game channels like "Pittsburgh Penguins vs ..."
        team_keywords.append(city.lower())

    if not team_keywords:
        return

    # Build regex patterns for matching
    patterns = []
    for kw in team_keywords:
        try:
            patterns.append(re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE))
        except re.error:
            continue

    def _matches_city(ch):
        """Check channel name, tvg_name, and source_name for city/team keywords."""
        texts = [ch.get("name") or "", ch.get("tvg_name") or "", ch.get("source_name") or ""]
        return any(p.search(t) for t in texts for p in patterns)

    # Find all groups that are Teams or league groups (Sports | NHL, Sports | NHL Teams, etc.)
    all_groups = list_groups()
    target_groups = [g for g in all_groups if re.search(
        r'(NHL|MLB|NFL|NBA|MLS|WNBA|NCAA|EPL|EFL|SPFL|CHL)\b', g["name"])]

    promoted_total = 0
    for g in target_groups:
        channels, total = get_group_channels(g["id"], offset=0, limit=10000)
        if not channels:
            continue

        # Split into matching (promote) and non-matching (keep order)
        matching = []
        rest = []
        for ch in channels:
            if _matches_city(ch):
                matching.append(ch["id"])
            else:
                rest.append(ch["id"])

        if not matching:
            continue

        # Reorder: matching channels first, then the rest. This is an AUTOMATIC
        # smart-group promotion, so don't freeze the group as custom.
        reorder_group_channels(g["id"], matching + rest, mark_custom=False)
        promoted_total += len(matching)
        log.info("Promoted %d city channels to top of '%s'", len(matching), g["name"])

    if promoted_total:
        log.info("Total city channel promotions: %d", promoted_total)


def _guide_bg_loop():
    """Background thread: refresh guide cache + smart groups + watchdog.

    Rebuilds the guide cache ONLY when the EPG file mtime has actually changed
    (vs. blindly every hour as before). The cache rebuild is the single most
    expensive thing this app does — skipping it when nothing changed
    dramatically lowers steady-state CPU and memory.
    """
    epg_path = EXPORT_DIR / "organized.xml"
    time.sleep(5)
    _refresh_guide_cache()
    try:
        _run_epg_watchdog_check()
    except Exception as exc:
        _record_backend_error("guide_bg.initial_watchdog", exc)
    try:
        _refresh_smart_groups()
    except Exception as exc:
        _record_backend_error("guide_bg.initial_smart_groups", exc)
    last_epg_mtime = epg_path.stat().st_mtime if epg_path.exists() else 0
    last_refresh = time.time()
    last_watchdog_mtime = 0.0
    # Poll cheaply, only do real work when something changed.
    SLEEP_S = 60
    WATCHDOG_MIN_INTERVAL_S = 300  # full XMLTV reparse is expensive (~121MB iterparse) — throttle
    last_watchdog_run = 0.0
    while True:
        time.sleep(SLEEP_S)
        # Run watchdog at most every WATCHDOG_MIN_INTERVAL_S, AND only when the
        # EPG file has actually changed since the last successful audit.
        now = time.time()
        cur_xml_mtime = 0.0
        try:
            xml_path = EXPORT_DIR / "organized.xml"
            if xml_path.exists():
                cur_xml_mtime = xml_path.stat().st_mtime
        except Exception:
            pass
        if (now - last_watchdog_run) >= WATCHDOG_MIN_INTERVAL_S and cur_xml_mtime != last_watchdog_mtime:
            try:
                _run_epg_watchdog_check()
                last_watchdog_mtime = cur_xml_mtime
            except Exception as exc:
                _record_backend_error("guide_bg.watchdog", exc)
            last_watchdog_run = now
        # Smart groups depend on channel/source state, not the EPG file, so
        # we still run them on a coarse cadence (every TTL window).
        if (time.time() - last_refresh) >= _GUIDE_CACHE_TTL:
            try:
                _refresh_smart_groups()
            except Exception as exc:
                _record_backend_error("guide_bg.smart_groups", exc)
            last_refresh = time.time()
        # Only rebuild the (expensive) guide cache if the EPG actually changed.
        try:
            cur_mtime = epg_path.stat().st_mtime if epg_path.exists() else 0
            if cur_mtime != last_epg_mtime:
                _refresh_guide_cache()
                last_epg_mtime = cur_mtime
        except Exception:
            pass


_last_source_refresh: float = 0

def _get_refresh_interval() -> int:
    """Return refresh interval in seconds from settings (default 60 minutes)."""
    raw = get_setting("refresh_interval_minutes")
    try:
        mins = int(raw) if raw else 60
        return max(mins, 5) * 60  # minimum 5 minutes
    except (ValueError, TypeError):
        return 3600

def _refresh_all_sources():
    """Sync channels from all active sources, preserving user edits. Detects additions/removals."""
    global _last_source_refresh
    import logging
    log = logging.getLogger("m3u_boss.scheduler")
    active_ids = get_active_source_ids()
    if not active_ids:
        return
    log.info("Scheduled sync: refreshing %d active source(s)...", len(active_ids))
    total_added = 0
    total_updated = 0
    total_removed = 0
    any_changed = False
    for sid in active_ids:
        src = get_source(sid)
        if not src:
            continue
        try:
            if src["type"] == "xc":
                channels, default_epg = import_channels_from_xc(
                    src["xc_server"], src["xc_username"], src["xc_password"], src["xc_output"], sid)
                epg = src.get("epg_url") or default_epg
                try:
                    acct = fetch_xc_account_info(src["xc_server"], src["xc_username"], src["xc_password"])
                    update_source(sid, expires_at=acct.get("expires_at"),
                                 max_connections=acct.get("max_connections"),
                                 active_connections=acct.get("active_connections"),
                                 status=acct.get("status", ""))
                except Exception:
                    pass
            elif src["type"] == "m3u":
                channels = import_channels_from_m3u(src["m3u_url"], sid, prefix_chno=_is_hdhr_source(src.get("name","")))
                epg = src.get("epg_url") or ""
            else:
                continue
            # Sync instead of clear+insert: preserves user edits
            result = sync_channels(sid, channels)
            log.info("Source %s (%s): +%d added, %d updated, -%d removed, %d revived",
                     sid, src.get("name",""), result["added"], result["updated"], result["removed"],
                     result.get("revived", 0))
            if result["added"] > 0 or result["removed"] > 0:
                any_changed = True
            if epg:
                update_source(sid, epg_url=epg)
            total_added += result["added"]
            total_updated += result["updated"]
            total_removed += result["removed"]
            update_source(sid, last_refreshed=datetime.now(timezone.utc).isoformat())
        except Exception as exc:
            log.warning("Refresh failed for source %s: %s", sid, exc)
    # Only re-run rules for NEW channels; existing keep their groups
    if any_changed:
        _assign_new_channels()
        _post_import_pipeline(sort_groups=False)
    # Existing station positions are intentionally frozen across refreshes.
    # SiriusXM/provider sorting remains available as an explicit editor action.
    # Always re-export (metadata like logos may have changed)
    _do_background_export()
    # Watchdog audit after export to catch guide regressions quickly.
    _maybe_run_epg_watchdog(force=True)
    # Refresh smart groups after export so EPG data is up-to-date
    try:
        _refresh_smart_groups()
    except Exception as exc:
        _record_backend_error("refresh_all.smart_groups", exc)
    _last_source_refresh = time.time()
    log.info("Sync complete: +%d added, %d updated, -%d removed across %d source(s)",
             total_added, total_updated, total_removed, len(active_ids))


def _source_refresh_loop():
    """Background thread: refresh all active sources on a configurable schedule."""
    global _last_source_refresh
    time.sleep(10)  # Let app finish starting
    _last_source_refresh = time.time()  # Don't refresh immediately on startup
    while True:
        interval = _get_refresh_interval()
        elapsed = time.time() - _last_source_refresh
        if elapsed >= interval:
            try:
                _refresh_all_sources()
            except Exception:
                pass
        # Run regular watchdog checks even between source refreshes.
        _maybe_run_epg_watchdog(force=False)
        # Check every 30 seconds whether it's time
        time.sleep(30)


@app.get("/api/_debug/threads")
def api_debug_threads():
    """TEMPORARY diagnostic: dump every Python thread's stack so we can find
    whatever's chewing CPU. Two consecutive snapshots ~1s apart help spot
    threads that moved (= active) vs sitting in same frame (= idle/blocked).
    """
    if not DEBUG_ENDPOINTS:
        raise HTTPException(404, "Not found")
    import sys, traceback, threading, time
    def snap():
        frames = sys._current_frames()
        by_ident = {t.ident: t for t in threading.enumerate()}
        out = []
        for tid, frame in frames.items():
            t = by_ident.get(tid)
            stack = traceback.format_stack(frame)
            out.append({
                "tid": tid,
                "name": getattr(t, "name", "?"),
                "daemon": getattr(t, "daemon", None),
                "top": stack[-1].strip() if stack else "",
                "stack": [s.rstrip() for s in stack[-12:]],
            })
        return out
    a = snap()
    time.sleep(1.0)
    b = snap()
    return {"snap_a": a, "snap_b": b}

@app.get("/api/refresh-status")
def api_refresh_status():
    interval = _get_refresh_interval()
    elapsed = time.time() - _last_source_refresh if _last_source_refresh else 0
    next_in = max(0, interval - elapsed)
    return {
        "interval_minutes": interval // 60,
        "last_refresh": datetime.fromtimestamp(_last_source_refresh, tz=timezone.utc).isoformat() if _last_source_refresh else None,
        "next_refresh_seconds": int(next_in),
    }

@app.post("/api/refresh-all")
def api_refresh_all():
    """Manually trigger a full source + EPG refresh."""
    _refresh_all_sources()
    return {"ok": True}


@app.get("/api/system/errors")
def api_system_errors(limit: int = Query(100, ge=1, le=500)):
    with _backend_errors_lock:
        rows = list(_backend_errors)
    rows = rows[-limit:]
    rows.reverse()
    return {"errors": rows, "count": len(rows)}


@app.delete("/api/system/errors")
def api_system_errors_clear():
    with _backend_errors_lock:
        _backend_errors.clear()
    return {"ok": True}


@app.get("/api/system/diagnostics-bundle")
def api_system_diagnostics_bundle():
    """Download a support snapshot designed to be safe for a public issue."""
    settings = get_all_settings()
    sources = [{
        "type": source.get("type"),
        "active": bool(source.get("is_active")),
        "auto_refresh": bool(source.get("auto_refresh")),
        "status": source.get("status"),
        "has_provider_epg": bool(source.get("epg_url")),
    } for source in list_sources()]
    with _backend_errors_lock:
        errors = list(_backend_errors)[-100:]
    bundle = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app_version": APP_VERSION,
        "python_version": platform.python_version(),
        "platform": platform.system(),
        "stats": channel_stats(source_id=get_active_source_ids()),
        "sources": sources,
        "epg_coverage": get_epg_coverage(),
        "export_status": dict(_export_status),
        "integrations": {
            "teamarr_enabled": settings.get("teamarr_enabled") == "1",
            "dispatcharr_configured": bool(settings.get("dispatcharr_url")),
            "notifications_enabled": settings.get("ntfy_enabled") == "1",
        },
        "recent_errors": errors,
    }
    return Response(
        json.dumps(bundle, indent=2), media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=m3u-boss-diagnostics.json",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/guide")
def api_guide(
    force: bool = False,
    rebuild: bool = False,
    max_programmes: int = Query(6, ge=1, le=400),
    full_desc: bool = Query(False),
    group_id: str | None = Query(None),
    groups_only: bool = Query(False),
):
    """Return cached EPG guide data.

    - force=true: refresh guide cache from current export XML.
    - rebuild=true: regenerate exports first, then refresh cache.
    """
    global _guide_cache, _guide_cache_time
    if rebuild:
        try:
            write_exports()
        except Exception:
            pass
    epg_path = EXPORT_DIR / "organized.xml"
    epg_is_newer = epg_path.exists() and epg_path.stat().st_mtime > _guide_cache_time
    if force or epg_is_newer or _guide_cache is None or (time.time() - _guide_cache_time) > _GUIDE_CACHE_TTL:
        _refresh_guide_cache()

    # When the EPG export was actually fetched + written. organized.xml is
    # rewritten by write_exports(), which re-downloads every EPG source — so
    # its mtime is the true "EPG synced" moment shown in the TV Guide.
    epg_synced = None
    try:
        if epg_path.exists():
            epg_synced = datetime.fromtimestamp(epg_path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        pass
    guide_built = (datetime.fromtimestamp(_guide_cache_time, tz=timezone.utc).isoformat()
                   if _guide_cache_time else None)

    if _guide_cache:
        payload = {**_guide_cache, "now": datetime.now(timezone.utc).isoformat()}
        if groups_only:
            return {
                "channels": [], "groups": payload.get("groups") or [], "now": payload["now"],
                "epg_synced": epg_synced, "guide_built": guide_built,
            }
        result = _compact_guide_payload(
            payload, max_programmes=max_programmes,
            include_full_desc=full_desc, group_id=group_id,
        )
        result["epg_synced"] = epg_synced
        result["guide_built"] = guide_built
        return result
    return {"channels": [], "now": datetime.now(timezone.utc).isoformat(),
            "epg_synced": epg_synced, "guide_built": guide_built}


# ── Stream proxy ──────────────────────────────────────────────────────
# Browsers can't fetch IPTV streams directly (CORS, redirects) and can't
# natively decode raw MPEG-TS, so the preview player proxies everything
# through here. HLS playlists get their segment/key URIs rewritten back
# through this proxy; raw MPEG-TS is streamed as-is (the frontend feeds it
# to mpegts.js, which decodes it via Media Source Extensions).

# A real player User-Agent — many IPTV providers reject unknown clients.
_STREAM_UA = "VLC/3.0.20 LibVLC/3.0.20"
_STREAM_TIMEOUT = 20
_STREAM_PROXY_SECRET = secrets.token_bytes(32)
_stream_target_lock = threading.Lock()
_stream_targets: dict[str, tuple[str, str, float]] = {}


def _remember_stream_target(channel_id: str, target_url: str) -> str:
    """Create an opaque segment key so upstream credentials never reach clients."""
    digest = hmac.new(
        _STREAM_PROXY_SECRET, f"{channel_id}\0{target_url}".encode(), hashlib.sha256
    ).hexdigest()
    now = time.time()
    with _stream_target_lock:
        if len(_stream_targets) > 10_000:
            expired = [key for key, (_, _, until) in _stream_targets.items() if until < now]
            for key in expired:
                _stream_targets.pop(key, None)
        _stream_targets[digest] = (channel_id, target_url, now + 6 * 3600)
    return digest


def _resolve_stream_target(channel_id: str, key: str) -> str:
    with _stream_target_lock:
        item = _stream_targets.get(key)
    if not item or item[0] != channel_id or item[2] < time.time():
        raise HTTPException(404, "Stream segment expired; reload the channel")
    return item[1]


def _looks_like_hls(content_type: str, url: str) -> bool:
    if "mpegurl" in (content_type or "").lower():
        return True
    return url.lower().split("?", 1)[0].endswith(".m3u8")


def _rewrite_hls_playlist(text: str, base_url: str, channel_id: str) -> str:
    """Rewrite every segment / sub-playlist / key URI back through the proxy."""
    def _proxied(raw_uri: str) -> str:
        abs_url = urljoin(base_url, raw_uri.strip())
        key = _remember_stream_target(channel_id, abs_url)
        return f"/api/stream/{channel_id}/seg?k={key}"

    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            # Rewrite URI="..." attributes (EXT-X-KEY, EXT-X-MEDIA, EXT-X-MAP).
            m = re.search(r'URI="([^"]*)"', stripped)
            if m:
                stripped = stripped[:m.start(1)] + _proxied(m.group(1)) + stripped[m.end(1):]
            out.append(stripped)
        else:
            out.append(_proxied(stripped))
    return "\n".join(out) + "\n"


def _open_upstream(target_url: str, range_header: str | None):
    """Open an upstream stream and read its first chunk.

    Returns (upstream_response, first_chunk, chunks_iterator) on success.
    Raises requests.RequestException on connection failure, non-200, or a
    failure reading the first chunk — i.e. every "this source is dead" signal,
    so the caller can decide whether to fail over to a backup source.
    """
    headers = {"User-Agent": _STREAM_UA, "Accept": "*/*"}
    if range_header:
        headers["Range"] = range_header
    upstream = requests.get(target_url, stream=True, timeout=_STREAM_TIMEOUT,
                            headers=headers, allow_redirects=True)
    try:
        upstream.raise_for_status()
        chunks = upstream.iter_content(chunk_size=64 * 1024)
        first = next(chunks, b"") or b""
    except requests.RequestException:
        upstream.close()
        raise
    return upstream, first, chunks


def _proxy_stream(target_url: str, channel_id: str, range_header: str | None):
    """Single-source proxy: fetch one upstream URL for the browser, rewriting
    HLS playlists inline. No failover (used for HLS segments / sub-playlists,
    where the playlist URL is already absolute and source-specific)."""
    return _proxy_stream_failover([target_url], channel_id, range_header)


def _proxy_stream_failover(source_urls: list[str], channel_id: str,
                           range_header: str | None):
    """Proxy a channel's live stream, failing over through ``source_urls`` in
    order. ``source_urls[0]`` is the primary; any remaining are backups tried
    ONLY when an earlier source fails to open (connection drop / non-200 /
    stalls reading the first chunk). Bounded by len(source_urls): once a source
    yields its first chunk the stream is considered live and we commit to it
    (a mid-stream upstream drop ends the response, exactly as before — we don't
    restart MSE buffers mid-play). With a single source this is byte-for-byte
    the original behavior.
    """
    upstream = first = chunks = None
    active_idx = 0
    last_err: Exception | None = None
    for idx, target_url in enumerate(source_urls):
        if not target_url:
            continue
        try:
            upstream, first, chunks = _open_upstream(target_url, range_header)
            active_idx = idx
            if idx > 0:
                log.warning("stream %s: source #%d failed, failed over to "
                            "backup source #%d", channel_id, idx - 1, idx)
            else:
                log.debug("stream %s: serving from primary source", channel_id)
            break
        except requests.RequestException as e:
            last_err = e
            if idx + 1 < len(source_urls):
                log.warning("stream %s: source #%d unavailable (%s); trying "
                            "next backup", channel_id, idx, e)
            continue
    if upstream is None:
        # Every configured source failed — same 502 surface as before.
        raise HTTPException(502, f"Stream unavailable: {_redact_text(last_err)}")

    final_url = upstream.url  # post-redirect — correct base for relative URIs
    content_type = upstream.headers.get("Content-Type", "")

    cors = {"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store"}
    # Surface which source is live so consumers / logs can see failover state.
    # (Absent header for primary keeps the happy path's response unchanged.)
    if active_idx > 0:
        cors["X-M3U-Active-Source"] = str(active_idx)

    # An HLS playlist starts with #EXTM3U; raw MPEG-TS starts with sync byte 0x47.
    is_playlist = first.lstrip()[:7] == b"#EXTM3U" or (
        _looks_like_hls(content_type, final_url) and first.lstrip()[:1] == b"#")

    if is_playlist:
        body = first + b"".join(chunks)
        upstream.close()
        rewritten = _rewrite_hls_playlist(body.decode("utf-8", "replace"),
                                          final_url, channel_id)
        return Response(rewritten, media_type="application/vnd.apple.mpegurl",
                        headers=cors)

    def _iter():
        try:
            if first:
                yield first
            for chunk in chunks:
                if chunk:
                    yield chunk
        except requests.RequestException:
            pass  # upstream dropped — just end the response
        finally:
            upstream.close()

    passthru = dict(cors)
    for h in ("Content-Range", "Accept-Ranges", "Content-Length"):
        if h in upstream.headers:
            passthru[h] = upstream.headers[h]
    return StreamingResponse(
        _iter(),
        status_code=upstream.status_code,
        media_type=content_type or "video/mp2t",
        headers=passthru,
    )


def _transcode_stream(source_url: str):
    """Transcode an upstream stream to browser-playable H.264/AAC MPEG-TS.

    OTA / HDHomeRun channels broadcast MPEG-2 video + AC-3 audio, which no
    browser can decode via Media Source Extensions. ffmpeg re-encodes them
    on the fly: deinterlace, cap at 720p to keep CPU sane, H.264 + stereo AAC.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-fflags", "+genpts",
        "-user_agent", _STREAM_UA,
        "-i", source_url,
        "-map", "0:v:0?", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", "yadif=0:-1:0,scale=w=-2:h='min(720,ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-f", "mpegts", "-muxdelay", "0", "-",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0)
    except FileNotFoundError:
        raise HTTPException(500, "ffmpeg is not installed in this container")

    def _iter():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            # Client gone (or stream ended) — kill ffmpeg so it doesn't linger.
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass
            # Reap it — without wait() the killed ffmpeg becomes a <defunct>
            # zombie (PID 1 here doesn't reap). init:true on the container is
            # the backstop, but reaping directly is cleaner and immediate.
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

    return StreamingResponse(
        _iter(),
        media_type="video/mp2t",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store"},
    )


def _channel_sources(ch: dict) -> list[str]:
    """Ordered upstream list for a channel: primary `url` first, then any
    configured `backup_urls` (JSON array). Returns just [primary] when no
    backups are set, so the failover path is a no-op for normal channels."""
    primary = (ch.get("url") or "").strip()
    sources = [primary] if primary else []
    raw = ch.get("backup_urls")
    if raw:
        try:
            for u in json.loads(raw):
                u = (u or "").strip()
                if u and u != primary and u not in sources:
                    sources.append(u)
        except (ValueError, TypeError):
            pass  # malformed backups must never break the primary path
    return sources


@app.get("/api/stream/{channel_id}")
def api_stream_proxy(channel_id: str, request: Request,
                     transcode: bool = Query(False)):
    """Proxy a channel's stream for the in-browser preview player.

    transcode=true re-encodes to H.264/AAC — used for OTA channels whose
    native codecs (MPEG-2 / AC-3) the browser can't decode.

    If the channel has backup_urls configured, the (non-transcoded) proxy
    fails over through them in order when the primary upstream is dead.
    """
    ch = get_channel(channel_id)
    if not ch or not ch.get("url"):
        raise HTTPException(404, "Channel not found")
    if transcode:
        return _transcode_stream(ch["url"])
    return _proxy_stream_failover(_channel_sources(ch), channel_id,
                                  request.headers.get("Range"))


@app.get("/api/stream/{channel_id}/seg")
def api_stream_segment(channel_id: str, request: Request, k: str = Query(...)):
    """Proxy an HLS segment / sub-playlist / key referenced by a proxied playlist."""
    target = _resolve_stream_target(channel_id, k)
    return _proxy_stream(target, channel_id, request.headers.get("Range"))


# In-memory cache for the patched M3U body. Key = (base_url, mtime). We rebuild
# only when the underlying file's mtime changes — turns "line-iterate 3MB on
# every poll" into "byte-slice once per real change".
_M3U_CACHE: dict = {}

def _maybe_304(request: Request, mtime: float, etag: str):
    """Honor If-None-Match / If-Modified-Since. Returns a 304 Response or None."""
    inm = request.headers.get("if-none-match")
    if inm and etag in inm:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=30, must-revalidate"})
    ims = request.headers.get("if-modified-since")
    if ims:
        try:
            from email.utils import parsedate_to_datetime
            ims_dt = parsedate_to_datetime(ims).timestamp()
            if int(mtime) <= int(ims_dt):
                return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=30, must-revalidate"})
        except Exception: pass
    return None


def _cache_headers(mtime: float, etag: str) -> dict:
    """30s client cache + must-revalidate. Lets polling clients send conditional
    GETs and get a cheap 304 when nothing's changed, instead of re-downloading."""
    from email.utils import formatdate
    return {
        "Cache-Control": "public, max-age=30, must-revalidate",
        "ETag": etag,
        "Last-Modified": formatdate(mtime, usegmt=True),
        "Vary": "Accept-Encoding",
    }


@app.get("/api/export/m3u")
@app.get("/m3u")
@app.head("/m3u")
def api_serve_m3u(request: Request, view: bool = Query(False), download: bool = Query(False)):
    p = EXPORT_DIR / "organized.m3u"
    epg_path = EXPORT_DIR / "organized.xml"
    if not p.exists():
        try: write_exports()
        except Exception: raise HTTPException(503, "No channels to export yet")
    if not p.exists(): raise HTTPException(503, "No channels to export yet")
    mtime = p.stat().st_mtime
    epg_ver = int(epg_path.stat().st_mtime) if epg_path.exists() else int(time.time())
    base = str(request.base_url).rstrip("/")
    feed_token = request.query_params.get("token", "")
    token_part = f"&token={quote(feed_token, safe='')}" if feed_token else ""
    epg_url = f"{base}/epg?v={epg_ver}{token_part}"
    cache_key = (base, int(mtime))
    etag = f'"m3u-{int(mtime)}-{hash(base) & 0xffffffff:x}"'

    # Conditional GET — bail before reading the file.
    not_mod = _maybe_304(request, mtime, etag)
    if not_mod is not None: return not_mod

    body = _M3U_CACHE.get(cache_key)
    if body is None:
        # Read once, patch the EXTM3U header in memory, keep the rest verbatim.
        with open(p, "rb") as f:
            raw = f.read()
        nl = raw.find(b"\n")
        first = raw[:nl].decode("utf-8", "replace") if nl != -1 else raw.decode("utf-8", "replace")
        rest  = raw[nl+1:] if nl != -1 else b""
        if first.startswith("#EXTM3U"):
            attrs = f'url-tvg="{epg_url}" x-tvg-url="{epg_url}"'
            patched_first = f"#EXTM3U {attrs}" + first[7:]
        else:
            patched_first = first
        body = (patched_first + "\n").encode("utf-8") + rest
        # Drop any stale entries for this base so we don't grow unbounded.
        _M3U_CACHE.clear()
        _M3U_CACHE[cache_key] = body

    headers = _cache_headers(mtime, etag)
    media = "audio/x-mpegurl"
    if view:
        media = "text/plain; charset=utf-8"
        headers["Content-Disposition"] = 'inline; filename="organized.m3u"'
    elif download:
        media = "application/octet-stream"
        headers["Content-Disposition"] = 'attachment; filename="organized.m3u"'
    else:
        headers["Content-Disposition"] = 'inline; filename="organized.m3u"'
    return Response(content=body, media_type=media, headers=headers)


@app.get("/api/feed-urls")
def api_feed_urls(request: Request):
    """Return credentialed player URLs only to an authenticated administrator."""
    base = (os.environ.get("M3U_BOSS_PUBLIC_URL") or str(request.base_url)).rstrip("/")
    suffix = f"?token={quote(_FEED_TOKEN, safe='')}" if _FEED_TOKEN else ""
    return {
        "m3u": f"{base}/m3u{suffix}",
        "epg": f"{base}/epg{suffix}",
        "protected": bool(_FEED_TOKEN),
    }


@app.get("/api/export/epg")
@app.get("/epg")
@app.get("/epg.xml")
@app.head("/epg")
@app.head("/epg.xml")
def api_serve_epg(request: Request, view: bool = Query(False), download: bool = Query(False)):
    p = EXPORT_DIR / "organized.xml"
    p_gz = EXPORT_DIR / "organized.xml.gz"
    if not p.exists():
        try: write_exports()
        except Exception: raise HTTPException(503, "No channels to export yet")
    if not p.exists(): raise HTTPException(503, "No channels to export yet")

    mtime = p.stat().st_mtime
    etag = f'"epg-{int(mtime)}-{p.stat().st_size:x}"'

    # Conditional GET — return 304 if the client has the current version.
    not_mod = _maybe_304(request, mtime, etag)
    if not_mod is not None: return not_mod

    headers = _cache_headers(mtime, etag)
    # Serve the pre-compressed .gz when the client supports it AND the user is
    # not in view/download mode (those want raw content for human inspection).
    # The .gz is 16MB vs 111MB raw — ~7× less data, ~7× less CPU per request.
    accept = (request.headers.get("accept-encoding") or "").lower()
    if not view and not download and "gzip" in accept and p_gz.exists():
        headers["Content-Encoding"] = "gzip"
        headers["Content-Disposition"] = 'inline; filename="organized.xml"'
        return FileResponse(str(p_gz), media_type="application/xml", headers=headers)

    if view:
        headers["Content-Disposition"] = 'inline; filename="organized.xml"'
        return FileResponse(str(p), media_type="text/plain; charset=utf-8", headers=headers)
    if download:
        headers["Content-Disposition"] = 'attachment; filename="organized.xml"'
        return FileResponse(str(p), media_type="application/octet-stream", headers=headers)
    headers["Content-Disposition"] = 'inline; filename="organized.xml"'
    return FileResponse(str(p), media_type="application/xml", headers=headers)


# ── Settings ──────────────────────────────────────────────────────

@app.get("/api/settings")
def api_settings():
    return _public_settings()

@app.patch("/api/settings")
def api_update_settings(p: SettingsUpdate):
    for k in ("teamarr_enabled","teamarr_username","teamarr_password","teamarr_output","teamarr_base_url",
              "refresh_interval_minutes", "epg_window_days",
              "dispatcharr_auto_refresh","dispatcharr_url","dispatcharr_username",
              "dispatcharr_password","dispatcharr_m3u_account_id","dispatcharr_epg_source_id",
              "ntfy_enabled","ntfy_url","ntfy_topic","ntfy_token"):
        v = getattr(p, k, None)
        # Empty secret inputs mean "leave the stored value unchanged". The API
        # never echoes secrets back, so the browser cannot accidentally erase a
        # configured credential when saving an unrelated integration option.
        if v is not None and not (k in _SECRET_SETTING_KEYS and v == ""):
            set_setting(k, v)
    return _public_settings()


# ── Smart Groups ──────────────────────────────────────────────────

@app.get("/api/smart-groups")
def api_list_smart_groups():
    return _get_smart_groups_config()

@app.get("/api/smart-groups/cities")
def api_smart_groups_cities():
    """Return the full city → teams database for the frontend."""
    return {city: data["teams"] for city, data in SPORTS_CITIES.items()}

@app.put("/api/smart-groups")
def api_save_smart_groups(settings: SmartGroupsSettings):
    """Save smart group settings and trigger an immediate refresh."""
    data = settings.model_dump()
    set_setting("smart_groups", json.dumps(data))
    # If disabled, clean up all shadows
    if not data.get("enabled"):
        delete_all_smart_group_channels()
    else:
        try:
            _refresh_smart_groups()
        except Exception as exc:
            _record_backend_error("smart_groups.save_refresh", exc)
    return data

@app.post("/api/smart-groups/refresh")
def api_refresh_smart_groups():
    """Manually trigger a smart group refresh."""
    _refresh_smart_groups()
    return {"ok": True}


# ── Backup / Restore ─────────────────────────────────────────────

@app.get("/api/backup")
def api_backup():
    data = export_all_data()
    return Response(
        content=json.dumps(data, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=m3uboss-backup.json",
            "Cache-Control": "no-store",
            "X-M3U-Boss-Contains-Secrets": "true",
        }
    )

@app.post("/api/restore")
async def api_restore(request: Request):
    body = await request.body()
    try: data = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}") from e
    if not isinstance(data, dict) or "sources" not in data:
        raise HTTPException(400, "Invalid backup format")
    validation = validate_backup_data(data)
    if not validation["valid"]:
        raise HTTPException(400, "Backup validation failed: " + "; ".join(validation["errors"]))
    import_all_data(data)
    return {"ok": True, "validation": validation}


# ═══════════════════════════════════════════════════════════════════
# TEAMARR — Xtream Codes Compatible API Proxy
# ═══════════════════════════════════════════════════════════════════
# This lets IPTV apps (TiviMate, XCIPTV, Smarters, etc.) connect
# directly to this server using XC credentials. They see only
# your curated, organized channel list.

def _teamarr_auth(username: str, password: str) -> bool:
    if get_setting("teamarr_enabled") != "1":
        return False
    return (secrets.compare_digest(username, get_setting("teamarr_username")) and
            secrets.compare_digest(password, get_setting("teamarr_password")))

@app.get("/player_api.php")
def teamarr_api(request: Request, username: str = "", password: str = "",
                action: str = "", category_id: str = "",
                stream_id: str = "", limit: int = Query(4, ge=1, le=100)):
    if not _teamarr_auth(username, password):
        raise HTTPException(403, "Authentication failed")

    settings = get_all_settings()
    output = settings.get("teamarr_output", "ts")

    if not action:
        # Account info
        now = datetime.now(timezone.utc)
        configured_base = settings.get("teamarr_base_url") or str(request.base_url)
        parsed = urlparse(configured_base if "://" in configured_base
                          else f"http://{configured_base}")
        protocol = parsed.scheme or "http"
        default_port = 443 if protocol == "https" else 80
        port = parsed.port or default_port
        return {
            "user_info": {
                "username": username,
                "password": password,
                "status": "Active",
                "exp_date": None,
                "is_trial": "0",
                "active_cons": "0",
                "created_at": str(int(now.timestamp())),
                "max_connections": "0",
            },
            "server_info": {
                "url": parsed.hostname or request.url.hostname or "localhost",
                "port": str(port if protocol == "http" else 80),
                "https_port": str(port if protocol == "https" else 443),
                "server_protocol": protocol,
                "timezone": "UTC",
                "timestamp_now": int(now.timestamp()),
                "time_now": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        }

    if action == "get_live_categories":
        groups = list_groups(source_id=get_active_source_ids())
        return [{"category_id": str(get_teamarr_category_id(g["id"])),
                 "category_name": g["name"],
                 "parent_id": 0} for g in groups if g.get("enabled", True) and g.get("teamarr")]

    if action == "get_live_streams":
        asid = get_active_source_ids()
        groups = list_groups(source_id=asid)
        enabled_groups = {g["id"]: (g, get_teamarr_category_id(g["id"]))
                          for g in groups if g.get("enabled", True) and g.get("teamarr")}
        channels = get_all_channels_raw(source_id=asid)
        group_order = {g["id"]: i for i, g in enumerate(groups)}
        channels.sort(key=lambda ch: (group_order.get(ch.get("group_id"), 999999),
                                      ch.get("sort_order", 0), ch.get("name", "")))
        chnos = assign_export_chnos([ch["id"] for ch in channels])
        result = []
        for ch in channels:
            if not ch.get("enabled"): continue
            gid = ch.get("group_id")
            if gid not in enabled_groups: continue
            numeric_category = enabled_groups[gid][1]
            if category_id and category_id != str(numeric_category):
                continue
            result.append({
                "num": chnos.get(ch["id"], 0),
                "name": ch.get("name") or "Unnamed",
                "stream_type": "live",
                "stream_id": get_teamarr_stream_id(ch["id"]),
                "stream_icon": ch.get("logo") or "",
                "epg_channel_id": ch.get("tvg_id") or "",
                "added": "",
                "category_id": str(numeric_category),
                "category_ids": [numeric_category],
                "tv_archive": 0,
                "direct_source": "",
                "tv_archive_duration": 0,
                "container_extension": output,
            })
        return result

    if action in {"get_short_epg", "get_simple_data_table"} and stream_id:
        try:
            channel = get_channel_by_teamarr_stream_id(int(stream_id))
        except (TypeError, ValueError):
            channel = None
        if not channel:
            return {"epg_listings": []}
        global _guide_cache
        if _guide_cache is None:
            _refresh_guide_cache()
        guide_channel = next((ch for ch in (_guide_cache or {}).get("channels", [])
                              if ch.get("id") == channel["id"]), None)
        listings = []
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for programme in (guide_channel or {}).get("programmes", []):
            try:
                start = datetime.fromisoformat(programme[_P_START])
                stop = datetime.fromisoformat(programme[_P_STOP])
            except (ValueError, TypeError):
                continue
            title = programme[_P_TITLE] or channel.get("name") or "Programme"
            desc = programme[_P_DESC] or ""
            start_ts, stop_ts = int(start.timestamp()), int(stop.timestamp())
            if stop_ts <= now_ts:
                continue
            listings.append({
                "id": f"{stream_id}-{start_ts}",
                "epg_id": channel.get("tvg_id") or "",
                "title": base64.b64encode(title.encode()).decode(),
                "lang": "en",
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": stop.strftime("%Y-%m-%d %H:%M:%S"),
                "description": base64.b64encode(desc.encode()).decode(),
                "channel_id": str(stream_id),
                "start_timestamp": str(start_ts),
                "stop_timestamp": str(stop_ts),
                "now_playing": 1 if start_ts <= now_ts < stop_ts else 0,
                "has_archive": 0,
            })
            if len(listings) >= limit:
                break
        return {"epg_listings": listings}

    # Live-only Teamarr intentionally returns valid empty arrays for the VOD
    # and series surfaces queried by many Xtream clients at login.
    if action in {"get_vod_categories", "get_vod_streams",
                  "get_series_categories", "get_series"}:
        return []
    return []


@app.get("/live/{username}/{password}/{stream_id_ext}")
def teamarr_stream(username: str, password: str, stream_id_ext: str):
    """Proxy live stream through Teamarr."""
    if not _teamarr_auth(username, password):
        raise HTTPException(403, "Authentication failed")

    # Parse Teamarr's durable numeric stream id from "12345.ts".
    sid = stream_id_ext.rsplit(".", 1)[0] if "." in stream_id_ext else stream_id_ext
    try:
        target = get_channel_by_teamarr_stream_id(int(sid))
    except (TypeError, ValueError):
        target = None

    if not target or not target.get("url"):
        raise HTTPException(404, "Stream not found")

    groups = {g["id"]: g for g in list_groups(source_id=get_active_source_ids())}
    group = groups.get(target.get("group_id"))
    if not target.get("enabled") or not group or not group.get("enabled") or not group.get("teamarr"):
        raise HTTPException(404, "Stream not found")
    return _proxy_stream_failover(_channel_sources(target), target["id"], None)


@app.get("/xmltv.php")
def teamarr_epg(request: Request, username: str = "", password: str = ""):
    """Serve the exported EPG XML for Teamarr clients."""
    if not _teamarr_auth(username, password):
        raise HTTPException(403, "Authentication failed")
    epg_path = EXPORT_DIR / "organized.xml"
    if not epg_path.exists():
        # Generate on the fly
        try:
            write_exports()
        except Exception as exc:
            _record_backend_error("teamarr.xmltv.generate", exc)
    if epg_path.exists():
        data = epg_path.read_bytes()
        accept = (request.headers.get("accept-encoding") or "")
        if "gzip" in accept:
            compressed = gzip.compress(data)
            return Response(content=compressed, media_type="application/xml",
                            headers={"Content-Encoding": "gzip"})
        return Response(content=data, media_type="application/xml")
    return Response(content='<?xml version="1.0" encoding="utf-8"?><tv></tv>',
                    media_type="application/xml")


@app.get("/teamarr.xml")
def teamarr_epg_simple(request: Request, username: str = "", password: str = ""):
    """Serve XMLTV at a clean URL using the same Teamarr credentials."""
    if not _teamarr_auth(username, password):
        raise HTTPException(403, "Authentication failed")
    epg_path = EXPORT_DIR / "organized.xml"
    if not epg_path.exists():
        try:
            write_exports()
        except Exception as exc:
            _record_backend_error("teamarr.simple.generate", exc)
    if epg_path.exists():
        data = epg_path.read_bytes()
        accept = (request.headers.get("accept-encoding") or "")
        if "gzip" in accept:
            compressed = gzip.compress(data)
            return Response(content=compressed, media_type="application/xml",
                            headers={"Content-Encoding": "gzip"})
        return Response(content=data, media_type="application/xml")
    return Response(content='<?xml version="1.0" encoding="utf-8"?><tv></tv>',
                    media_type="application/xml")
