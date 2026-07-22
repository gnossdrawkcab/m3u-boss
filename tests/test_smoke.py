"""Smoke / regression tests for m3u-boss.

Run from the repo root:  python -m pytest -q
(In the container:        docker exec m3u-boss python -m pytest tests -q)

These cover the modules extracted out of main.py during the refactor
(app/epg.py, app/rules.py, app/importers.py) plus a few HTTP smoke checks.
"""

import base64
from xml.etree import ElementTree as ET

import pytest

from app.epg import (
    _epg_id_is_foreign, _canonical_channel_name, _extract_callsign,
    _epg_name_matches, enrich_tvg_ids,
)
from app.rules import matches_rule
from app.importers import parse_server, build_stream_url, parse_extinf_attrs


# ── app/epg.py — pure helpers ────────────────────────────────────────

def test_epg_id_is_foreign():
    assert _epg_id_is_foreign("quest.uk")
    assert _epg_id_is_foreign("Family.al")
    assert _epg_id_is_foreign("e!entertainment.ca")
    assert not _epg_id_is_foreign("metv.us")
    assert not _epg_id_is_foreign("468596")        # numeric provider id
    assert not _epg_id_is_foreign("KEDT")          # no suffix at all


def test_canonical_channel_name():
    assert _canonical_channel_name("16.1 ION-HD") == "ion"
    assert _canonical_channel_name("CBS  KDKA") == "cbs kdka"


def test_extract_callsign():
    assert _extract_callsign("2.1 KDKA-HD") == "kdka"
    assert _extract_callsign("WTAE") == "wtae"


def test_epg_name_matches():
    assert _epg_name_matches("ESPN", ["espn"])
    assert not _epg_name_matches("ESPN", ["fox sports"])


# ── app/epg.py — enrich_tvg_ids (the antenna-EPG fix) ────────────────

def _epg(xml):
    return ET.fromstring(xml)


def test_ota_channel_rejects_foreign_guide(monkeypatch):
    """A US OTA channel must not adopt a foreign-country EPG id."""
    import app.epg as epg
    monkeypatch.setattr(epg, "list_groups", lambda: [])
    monkeypatch.setattr(epg, "update_channel", lambda *a, **k: None)

    root = _epg('<tv><channel id="quest.uk">'
                '<display-name>Quest</display-name></channel></tv>')
    chans = [{"id": "c1", "name": "40.4 Quest", "tvg_id": "",
              "enabled": True, "group_id": None}]
    selected = enrich_tvg_ids(chans, root)

    assert chans[0]["tvg_id"] == ""          # foreign id rejected
    assert "quest.uk" not in selected


def test_ota_channel_matches_callsign(monkeypatch):
    """A US OTA channel matches the right station by FCC call sign."""
    import app.epg as epg
    monkeypatch.setattr(epg, "list_groups", lambda: [])
    monkeypatch.setattr(epg, "update_channel", lambda *a, **k: None)

    root = _epg('<tv><channel id="KDKA.us">'
                '<display-name>KDKA</display-name></channel></tv>')
    chans = [{"id": "c1", "name": "2.1 KDKA-HD", "tvg_id": "",
              "enabled": True, "group_id": None}]
    selected = enrich_tvg_ids(chans, root)

    assert chans[0]["tvg_id"] == "KDKA.us"
    assert "KDKA.us" in selected


# ── app/rules.py ─────────────────────────────────────────────────────

def test_matches_rule():
    ch = {"source_group": "24/7 NHL", "source_name": "Penguins HD"}
    assert matches_rule(ch, {"field": "source_group", "pattern": "nhl",
                             "match_type": "contains"})
    assert not matches_rule(ch, {"field": "source_group", "pattern": "nba",
                                 "match_type": "contains"})
    assert matches_rule(ch, {"field": "channel_name", "pattern": "pengu",
                             "match_type": "starts_with"})


# ── app/importers.py ─────────────────────────────────────────────────

def test_parse_server():
    assert parse_server("example.com") == "http://example.com"
    assert parse_server("https://x.com/") == "https://x.com"
    with pytest.raises(ValueError):
        parse_server("   ")


def test_build_stream_url():
    assert build_stream_url("http://s", "u", "p", 42, "ts") == \
        "http://s/live/u/p/42.ts"
    assert build_stream_url("http://s", "u", "p", 42, "m3u8").endswith(".m3u8")


def test_parse_extinf_attrs():
    attrs, display = parse_extinf_attrs(
        '#EXTINF:-1 tvg-id="abc" group-title="News",Channel One')
    assert attrs["tvg-id"] == "abc"
    assert attrs["group-title"] == "News"
    assert display == "Channel One"


def test_promote_tunarr_episode_titles():
    from app.main import _promote_tunarr_episode_titles

    tv = ET.fromstring(
        '<programme channel="tv.tunarr.com"><title>Friends</title>'
        '<sub-title>The One with the Test</sub-title>'
        '<episode-num>S2E3</episode-num></programme>'
    )
    movie_mix = ET.fromstring(
        '<programme channel="movies.tunarr.com"><title>Miss Austen</title>'
        '<sub-title>Episode 1</sub-title><episode-num>S1E1</episode-num></programme>'
    )
    selected = [
        ({"name": "24/7 TV"}, {"id": "tv", "tvg_id": "tv.tunarr.com"}),
        ({"name": "24/7 Movies"}, {"id": "movies", "tvg_id": "movies.tunarr.com"}),
    ]
    _promote_tunarr_episode_titles([tv, movie_mix], selected, {})

    assert tv.findtext("title") == "S2E3 · The One with the Test"
    assert tv.findtext("sub-title") == "The One with the Test"
    assert movie_mix.findtext("title") == "Miss Austen · S1E1 · Episode 1"


def test_public_settings_are_small_and_hide_internal_state():
    from app.main import _public_settings

    public = _public_settings({
        "refresh_interval_minutes": "60",
        "dispatcharr_password": "secret",
        "epg_chno_baseline": "x" * 100_000,
        "epg_export_tvg_id_map": "also-internal",
    })

    assert public["refresh_interval_minutes"] == "60"
    assert public["dispatcharr_password"] == ""
    assert public["dispatcharr_password_configured"] is True
    assert "epg_chno_baseline" not in public
    assert "epg_export_tvg_id_map" not in public


def test_compact_guide_payload_scopes_to_one_group():
    from app.main import _compact_guide_payload

    data = {
        "now": "2026-07-21T12:00:00+00:00",
        "groups": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "channels": [
            {"id": "1", "name": "One", "group_id": "a", "programmes": []},
            {"id": "2", "name": "Two", "group_id": "b", "programmes": []},
        ],
    }

    result = _compact_guide_payload(data, max_programmes=6, group_id="b")
    assert [channel["id"] for channel in result["channels"]] == ["2"]
    assert result["groups"] == data["groups"]


def test_current_group_listings_returns_current_and_next_only():
    from datetime import datetime, timezone
    from app.main import _current_group_listings

    data = {"channels": [{
        "id": "one", "name": "Channel One", "group_id": "g1",
        "programmes": [
            ("2026-07-22T11:00:00+00:00", "2026-07-22T12:00:00+00:00", "Old", "", "", ""),
            ("2026-07-22T12:00:00+00:00", "2026-07-22T13:00:00+00:00", "Live Game", "Penguins", "", "real details"),
            ("2026-07-22T13:00:00+00:00", "2026-07-22T14:00:00+00:00", "Postgame", "", "", ""),
        ],
    }, {"id": "other", "group_id": "g2", "programmes": []}]}
    result = _current_group_listings(
        data, "g1", datetime(2026, 7, 22, 12, 30, tzinfo=timezone.utc))

    assert set(result["listings"]) == {"one"}
    assert result["listings"]["one"]["current"]["title"] == "Live Game"
    assert result["listings"]["one"]["next"]["title"] == "Postgame"


# ── HTTP smoke (needs the full app environment) ──────────────────────

def test_api_smoke():
    """Key read-only endpoints respond without a server error."""
    try:
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db import init_db
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"app environment unavailable: {exc}")
    init_db()
    client = TestClient(app)
    token = base64.b64encode(b"admin:test-only-password").decode()
    headers = {"Authorization": f"Basic {token}"}
    for path in ["/", "/api/dashboard", "/api/groups", "/api/sources"]:
        assert client.get(path, headers=headers).status_code == 200, path
