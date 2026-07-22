"""EPG (XMLTV) parsing and tvg-id matching.

Extracted from main.py. This module depends only on the standard library,
``requests`` and ``app.db`` — it never imports ``app.main``, so there is no
circular-import risk.
"""

import copy
import gzip
import re
from xml.etree import ElementTree as ET

import requests

from app.db import list_groups, update_channel


def parse_epg(url):
    r = requests.get(url, timeout=120); r.raise_for_status()
    data = r.content
    if url.endswith('.gz'):
        data = gzip.decompress(data)
    return ET.fromstring(data)

def _extract_callsign(name):
    """Extract a broadcast call sign from an HDHR-style channel name.
    '2.1 KDKA-HD' -> 'kdka', '11.1 WPXI-HD' -> 'wpxi', 'WTAE' -> 'wtae'
    Strips leading channel numbers and trailing suffixes like -HD, -DT, -LD, -CD."""
    s = re.sub(r'^\d+(\.\d+)?\s+', '', name.strip())  # strip leading "2.1 "
    s = re.sub(r'[- ]*(HD|DT|LD|CD|SD)\b', '', s, flags=re.IGNORECASE)  # strip -HD etc
    s = s.strip().lower()
    return s if s else None

def _extract_epg_callsign(epg_id):
    """Extract a canonical FCC callsign from an EPG channel id.
    'KDKADT1.us@SD' -> 'kdka', 'WPXIDT2.us@SD' -> 'wpxi',
    'StartTV.us' -> 'starttv', 'KDKA.us' -> 'kdka'.
    Strips trailing DT/LD/CD + digit suffixes and everything after the first dot."""
    prefix = epg_id.split(".")[0]  # 'KDKADT1'
    # Strip trailing DT/LD/CD + digits (e.g. DT1, LD3, CD2)
    cleaned = re.sub(r'(DT|LD|CD)\d*$', '', prefix, flags=re.IGNORECASE)
    return cleaned.lower() if cleaned else None

def _extract_display_callsign(display_name):
    """Extract FCC callsign from an EPG display name.
    'CBS (KDKA-DT1) Pittsburgh, PA' -> 'kdka'
    'NBC (WPXI-DT1) Pittsburgh, PA' -> 'wpxi'
    'KDKA' -> 'kdka'
    Looks for a 3-4 letter callsign in parentheses or as the whole name."""
    # Try to find callsign in parentheses: (KDKA-DT1) or (WPXI)
    m = re.search(r'\(([A-Z]{3,5})(?:[- ]?(?:DT|LD|CD|TV)\d*)?\)', display_name, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return None

def _epg_id_is_foreign(epg_id):
    """True if an EPG channel id carries a non-US country suffix — e.g. '.uk',
    '.al', '.ca', '.gr'. Used to stop US OTA channels from borrowing a guide
    from an unrelated foreign station that happens to share a generic name."""
    suffix = epg_id.rsplit(".", 1)[-1].lower() if "." in epg_id else ""
    return len(suffix) == 2 and suffix != "us"

def _canonical_channel_name(name):
    """Normalize a channel/EPG display name for robust matching."""
    s = (name or "").strip()
    s = re.sub(r'^\d+(?:\.\d+)?\s+', '', s)
    s = s.replace("&", " and ")
    s = re.sub(r'[-_]+', ' ', s)
    s = re.sub(r'\b(HD|SD|DT|LD|CD)\b', ' ', s, flags=re.IGNORECASE)
    s = re.sub(r'\bTV\b$', '', s, flags=re.IGNORECASE)
    s = re.sub(r'[^a-z0-9]+', ' ', s.lower())
    return re.sub(r'\s+', ' ', s).strip()


def _channel_identity_key(name):
    """Normalize names for guide-identity comparison, ignoring quality markers.

    This helps keep HD/SD/UHD variants on the same guide while still treating
    genuinely different event feeds as distinct.
    """
    s = _canonical_channel_name(name)
    s = re.sub(r'\b(uhd|fhd|hd|sd|4k|hevc|hdr|atmos|stereo|surround|max|eng)\b', ' ', s)
    s = re.sub(r'\b\d\s*1\b', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def _epg_name_matches(channel_key, epg_keys):
    if not channel_key or not epg_keys:
        return False
    chan = _canonical_channel_name(channel_key)
    chan_compact = chan.replace(" ", "")
    for key in epg_keys:
        epg = _canonical_channel_name(key)
        epg_compact = epg.replace(" ", "")
        if chan == epg or chan_compact == epg_compact:
            return True
        if len(chan_compact) >= 4 and len(epg_compact) >= 4:
            if chan_compact in epg_compact or epg_compact in chan_compact:
                return True
    return False


_LOOP_CHANNEL_RE = re.compile(r"\b(24\s*/\s*7|24x7|247)\b", re.IGNORECASE)


# OTA sub-channel ("diginet") names arrive heavily abbreviated — "39.5 OAN",
# "45.6 Hrtlnd", "61.7 NEWSMX2" — so they match neither by FCC call sign nor by
# XMLTV display name. These are national networks with a single nationwide
# feed, so mapping the abbreviation straight to a known guide id is safe and
# timezone-correct. Keys are lowercase name tokens; values are EPG channel ids
# (the id only applies when it exists in the EPG source being processed, so a
# guide that ships under a different id in another source still wins there).
OTA_DIGINET_ALIASES = {
    "oan": "oneamericanews.us",
    "newsmx2": "newsmax2.us",
    "hrtlnd": "heartland.us", "heartld": "heartland.us",
    "trucrim": "truecrimenetwork.us", "trucrme": "truecrimenetwork.us",
    "amvoice": "realamerica'svoice.us", "amvoce": "realamerica'svoice.us",
    "sbn": "SBNtv.us",
    "wxnatn": "WeatherNation.pluto",
    "wpnt": "MYWPNT.us",
}


def _ota_diginet_alias(name):
    """Map an abbreviated OTA sub-channel name to a known diginet EPG id."""
    stripped = re.sub(r"^\d+(?:\.\d+)?\s*", "", (name or "").strip())
    for tok in re.split(r"[\s\-_]+", stripped.lower()):
        if tok in OTA_DIGINET_ALIASES:
            return OTA_DIGINET_ALIASES[tok]
    return ""


def _channel_is_loop_like(ch):
    """Return True for always-on loop channels that should keep source guide IDs."""
    group_name = (ch.get("source_group") or "")
    name = (ch.get("tvg_name") or ch.get("source_name") or ch.get("name") or "")
    tvg = (ch.get("tvg_id") or "")
    orig_tvg = (ch.get("original_tvg_id") or "")
    blob = " ".join((group_name, name, tvg, orig_tvg))
    if "ersatztv.org" in blob.lower():
        return True
    return bool(_LOOP_CHANNEL_RE.search(blob))


def enrich_tvg_ids(channels, epg_root, require_nonblank_programmes=False):
    name_to_id = {}
    compact_to_id = {}
    id_to_names = {}
    # Also build a mapping keyed by channel number for OTA/HDHR matching
    # EPG display names like "KDKA CBS 2.1" → extract number suffix + call sign
    chnum_to_id = {}  # e.g. "2.1" -> "KDKA.us"
    # Build call-sign map from EPG: extract call sign from EPG id and display name
    callsign_to_id = {}  # e.g. "kdka" -> "KDKA.us"
    # Set of all channel IDs actually present in this EPG source
    epg_channel_ids = {n.attrib.get("id") for n in epg_root.findall("channel")}
    channel_has_text = {}

    if require_nonblank_programmes:
        for prog in epg_root.findall("programme"):
            cid = (prog.attrib.get("channel") or "").strip()
            if not cid:
                continue
            title_nodes = prog.findall("title")
            desc_nodes = prog.findall("desc")
            has_text = any((t.text or "").strip() for t in title_nodes) or any((d.text or "").strip() for d in desc_nodes)
            if has_text:
                channel_has_text[cid] = True
    for node in epg_root.findall("channel"):
        cid = node.attrib.get("id","")
        display_names = [(dn.text or "") for dn in node.findall("display-name")]
        text = display_names[0] if display_names else ""
        if cid:
            id_to_names.setdefault(cid, set())
        for dn_text in display_names:
            dn_norm = _canonical_channel_name(dn_text)
            dn_compact = dn_norm.replace(" ", "")
            if cid and dn_norm:
                id_to_names[cid].add(dn_norm)
                if dn_norm not in name_to_id:
                    name_to_id[dn_norm] = cid
                if dn_compact and dn_compact not in compact_to_id:
                    compact_to_id[dn_compact] = cid
            # Extract trailing channel number like "2.1" from any display alias
            if cid and dn_text:
                m = re.search(r'(\d+\.\d+)\s*$', dn_text.strip())
                if m and m.group(1) not in chnum_to_id:
                    chnum_to_id[m.group(1)] = cid
        # Build call-sign lookup from EPG channel id (e.g. "KDKA.us" -> "kdka")
        # and from EPG display names (e.g. "KDKA" -> "kdka")
        if cid:
            # From EPG id: take part before first dot (raw)
            id_prefix = cid.split(".")[0].lower()
            if id_prefix and id_prefix not in callsign_to_id:
                callsign_to_id[id_prefix] = cid
            # From EPG id: extract FCC callsign (strip DT1/LD2/CD3 suffixes)
            # e.g. "KDKADT1.us@SD" -> "kdka"
            epg_cs = _extract_epg_callsign(cid)
            if epg_cs and epg_cs not in callsign_to_id:
                callsign_to_id[epg_cs] = cid
            for dn_text in display_names:
                if not dn_text:
                    continue
                cs = _extract_callsign(dn_text)
                if cs and cs not in callsign_to_id:
                    callsign_to_id[cs] = cid
                dcs = _extract_display_callsign(dn_text)
                if dcs and dcs not in callsign_to_id:
                    callsign_to_id[dcs] = cid
    selected = set()
    groups_map = {g["id"]: g for g in list_groups()}
    for ch in channels:
        gid = ch.get("group_id")
        grp = groups_map.get(gid)
        if grp and not grp.get("enabled", True): continue
        if not ch.get("enabled", True): continue
        tvg = (ch.get("tvg_id") or "").strip()
        orig_tvg = (ch.get("original_tvg_id") or "").strip()
        name_for_match = ch.get("tvg_name") or ch.get("source_name") or ch.get("name", "")
        key = _canonical_channel_name(name_for_match)
        key_compact = key.replace(" ", "")

        guessed = ""
        needs_guess = not tvg
        if orig_tvg and orig_tvg in epg_channel_ids:
            if tvg != orig_tvg:
                update_channel(ch["id"], tvg_id=orig_tvg)
                ch["tvg_id"] = orig_tvg
                tvg = orig_tvg
            selected.add(orig_tvg)
            continue
        if tvg and tvg in epg_channel_ids:
            # Trust source-provided ids when they exist in this provider EPG.
            # Event/sports slot channels often change names faster than XMLTV
            # display names, so name-based "repair" causes bad cross-matches.
            selected.add(tvg)
            continue
        if tvg:
            # In fallback EPG passes, don't overwrite a provider-assigned ID via
            # name matching. Channels like ErsatzTV 24/7 loops have source-specific
            # IDs (e.g. C1.145.ersatztv.org) that only exist in their own EPG.
            # Borrowing a name match from Pluto/broadcast guides gives wrong data
            # since the loop schedule differs from live broadcast schedules.
            # Only allow name guessing if the channel never had a provider ID.
            keep_source_id = bool(orig_tvg) or _channel_is_loop_like(ch)
            needs_guess = not (require_nonblank_programmes and keep_source_id)

        if needs_guess:
            ch_name = ch.get("name", "") or ""
            # OTA / antenna channels: a number-prefixed name ("16.1 ION") or an
            # explicit channel number. Their guides are market-specific, so they
            # get a stricter, call-sign-first match — never a loose brand match
            # or a channel-number match, both of which cross-wired them to
            # unrelated (often foreign) stations.
            is_ota = bool(re.match(r'^\d+\.\d+\s', ch_name)) or bool(
                (ch.get("channel_number") or "").strip())

            if is_ota:
                # Strategy 1: FCC call sign — the reliable signal for broadcast
                # channels. Tried first so a loose name hit can't pre-empt it.
                cs = _extract_callsign(ch_name or ch.get("tvg_name", "") or "")
                if cs and cs in callsign_to_id:
                    guessed = callsign_to_id[cs]
                # Strategy 2: exact / compact display-name match.
                if not guessed:
                    guessed = name_to_id.get(key, "")
                if not guessed and key_compact:
                    guessed = compact_to_id.get(key_compact, "")
                # Strategy 3: known diginet abbreviation (OAN, Heartland,
                # Newsmax 2, ...). Compressed OTA sub-channel names match
                # neither call sign nor display name, so map them explicitly.
                if not guessed:
                    alias = _ota_diginet_alias(ch_name)
                    if alias and alias in epg_channel_ids:
                        guessed = alias
                # Never let a US OTA channel adopt a foreign-country guide.
                if guessed and _epg_id_is_foreign(guessed):
                    guessed = ""
            else:
                # Strategy 1: exact or compact name match
                if not guessed:
                    guessed = name_to_id.get(key,"")
                if not guessed and key_compact:
                    guessed = compact_to_id.get(key_compact, "")
                # Strategy 1b: unique loose brand match for abbreviated labels.
                if not guessed and len(key_compact) >= 4:
                    loose = {cid for comp, cid in compact_to_id.items() if key_compact in comp or comp in key_compact}
                    if len(loose) == 1:
                        guessed = next(iter(loose))
                # Strategy 2: match by call sign (strip number prefix + HD suffix)
                if not guessed:
                    cs = _extract_callsign(ch_name or ch.get("tvg_name","") or "")
                    if cs and cs in callsign_to_id:
                        guessed = callsign_to_id[cs]
            if guessed and guessed != tvg:
                if require_nonblank_programmes and not channel_has_text.get(guessed, False):
                    guessed = ""
                # OTA/antenna channels: never downgrade a real call-sign id
                # (e.g. "wpxi.us") to a bare numeric id (e.g. epg.pw's
                # "468942"). Numeric OTA guides carry time-shifted local
                # listings; this guard makes the match converge on the good
                # guide regardless of the order EPG sources are processed in.
                elif is_ota and tvg and not tvg.isdigit() and guessed.isdigit():
                    guessed = ""
            if guessed and guessed != tvg:
                update_channel(ch["id"], tvg_id=guessed)
                ch["tvg_id"] = guessed
                tvg = guessed
        if tvg and tvg in epg_channel_ids:
            selected.add(tvg)
    return selected

def build_filtered_epg(epg_root, selected):
    out = ET.Element("tv", epg_root.attrib)
    for n in epg_root.findall("channel"):
        if n.attrib.get("id") in selected: out.append(copy.deepcopy(n))
    for n in epg_root.findall("programme"):
        if n.attrib.get("channel") in selected: out.append(copy.deepcopy(n))
    return out
