"""Source importers: HTTP fetch helpers and XC / M3U playlist parsing.

Extracted from main.py. Depends only on the standard library and ``requests`` —
never imports ``app.main``, so there is no circular-import risk.
"""

import re
import uuid
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse, urlencode, parse_qs, urlunparse

import requests


# ═══════════════════════════════════════════════════════════════════
# Fetch helpers
# ═══════════════════════════════════════════════════════════════════

def fetch_json(url, timeout=40):
    r = requests.get(url, timeout=timeout); r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected response format.")
    return data

def fetch_text(url, timeout=120):
    r = requests.get(url, timeout=timeout); r.raise_for_status()
    # `requests` defaults charset-less text/* responses to ISO-8859-1 (per the
    # HTTP spec), which mojibakes UTF-8 playlists — e.g. "·" (U+00B7, bytes
    # C2 B7) decoded as Latin-1 becomes "Â·". IPTV M3Us are UTF-8 in practice,
    # so force it when the server didn't declare a non-Latin-1 charset.
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "latin-1", "ascii"):
        r.encoding = "utf-8"
    return r.text

def parse_server(server):
    server = server.strip().rstrip("/")
    if not server: raise ValueError("Server is required.")
    if not server.startswith("http://") and not server.startswith("https://"):
        server = f"http://{server}"
    return server

def normalize_name(v):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", v.lower())).strip()

def _is_hdhr_source(name):
    return bool(name and re.search(r'hdhr|hdhomerun', name, re.IGNORECASE))


# ═══════════════════════════════════════════════════════════════════
# XC import
# ═══════════════════════════════════════════════════════════════════

def build_stream_url(server, user, pw, stream_id, output):
    ext = "ts" if output == "ts" else "m3u8"
    return f"{server}/live/{user}/{pw}/{stream_id}.{ext}"

def import_channels_from_xc(server, username, password, output, source_id):
    safe = parse_server(server)
    base = f"{safe}/player_api.php?username={username}&password={password}"
    categories = fetch_json(f"{base}&action=get_live_categories")
    streams = fetch_json(f"{base}&action=get_live_streams")
    cat_map = {str(c.get("category_id","")): c.get("category_name") or "Ungrouped" for c in categories}
    channels = []
    for i, s in enumerate(streams):
        cid = str(s.get("category_id",""))
        channels.append({
            "id": str(uuid.uuid4()), "source_id": source_id,
            "stream_id": s.get("stream_id"),
            "name": s.get("name") or "Unnamed",
            "source_name": s.get("name") or "Unnamed",
            "source_group": cat_map.get(cid, "Ungrouped"),
            "tvg_id": s.get("epg_channel_id") or "",
            "tvg_name": s.get("name") or "",
            "logo": s.get("stream_icon") or "",
            "url": build_stream_url(safe, username, password, s.get("stream_id"), output),
            "channel_number": str(s.get("num") or ""),
            "enabled": True, "group_id": None, "sort_order": 0,
            "source_order": i,
        })
    epg_url = f"{safe}/xmltv.php?username={username}&password={password}"
    return channels, epg_url

def fetch_xc_account_info(server, username, password):
    safe = parse_server(server)
    try:
        r = requests.get(f"{safe}/player_api.php?username={username}&password={password}", timeout=20)
        r.raise_for_status(); data = r.json()
    except Exception:
        return {}
    ui = data.get("user_info") or {}
    result = {"status": ui.get("status") or "", "max_connections": None,
              "active_connections": None, "expires_at": None}
    try: result["max_connections"] = int(ui.get("max_connections") or 0)
    except (ValueError, TypeError): pass
    try: result["active_connections"] = int(ui.get("active_cons") or 0)
    except (ValueError, TypeError): pass
    exp = ui.get("exp_date")
    if exp:
        try: result["expires_at"] = datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError): pass
    return result


# ═══════════════════════════════════════════════════════════════════
# M3U import
# ═══════════════════════════════════════════════════════════════════

def parse_extinf_attrs(line):
    attrs = {}
    split_at = len(line)
    in_quote = False
    for idx, ch in enumerate(line):
        if ch == '"':
            in_quote = not in_quote
        elif ch == "," and not in_quote:
            split_at = idx
            break
    left = line[:split_at]
    for k, v in re.findall(r'([\w-]+)="([^"]*)"', left):
        attrs[k.lower()] = unquote(v)
    display = line[split_at + 1:].strip() if split_at < len(line) else ""
    return attrs, display

def _prefix_chno(tvg_name, chno):
    """Prepend channel number to tvg_name if present and not already there."""
    if chno and tvg_name and not tvg_name.startswith(chno):
        return f"{chno} {tvg_name}"
    return tvg_name

def import_channels_from_m3u(m3u_url, source_id, prefix_chno=False):
    text = fetch_text(m3u_url.strip())
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    channels = []
    pending = None
    for line in lines:
        if line.startswith("#EXTINF"):
            attrs, display = parse_extinf_attrs(line)
            # Extract channel number from tvg-chno, channel-number, or display name prefix
            chno = attrs.get("tvg-chno") or attrs.get("channel-number") or ""
            if not chno and display:
                # Try to extract leading number like "2.1 KDKA-HD" -> "2.1"
                m = re.match(r'^(\d+(?:\.\d+)?)\s', display)
                if m: chno = m.group(1)
            tvg_name = attrs.get("tvg-name","")
            if prefix_chno:
                tvg_name = _prefix_chno(tvg_name, chno)
            pending = {
                "id": str(uuid.uuid4()), "source_id": source_id,
                "stream_id": None,
                "name": display or attrs.get("tvg-name") or "Unnamed",
                "source_name": display or attrs.get("tvg-name") or "Unnamed",
                "source_group": attrs.get("group-title") or "Ungrouped",
                "tvg_id": attrs.get("tvg-id",""),
                "tvg_name": tvg_name,
                "logo": attrs.get("tvg-logo",""), "url": "",
                "channel_number": chno,
                "enabled": True, "group_id": None, "sort_order": 0,
            }
            continue
        if line.startswith("#"): continue
        if pending:
            url = line
            # Strip ?streamMode= from Tunarr stream URLs so the stored URL is
            # always the bare channel URL. Tunarr respects its channel-configured
            # mode when no override is present, and this keeps the URL stable
            # across stream-mode changes so sync_channels updates in-place.
            if "/stream/channels/" in url and "streamMode=" in url:
                p = urlparse(url)
                qs = {k: v for k, v in parse_qs(p.query).items() if k != "streamMode"}
                url = urlunparse(p._replace(query=urlencode(qs, doseq=True)))
            pending["url"] = url
            pending["source_order"] = len(channels)
            channels.append(pending)
            pending = None
    return channels
