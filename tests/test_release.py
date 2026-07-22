"""Release-surface tests: authentication, migrations, and Teamarr contract."""

import base64
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


def _fresh_db(monkeypatch, tmp_path):
    import app.db as db

    old = getattr(db._local, "conn", None)
    if old is not None:
        old.close()
        del db._local.conn
    monkeypatch.setenv("M3U_BOSS_DB", str(tmp_path / "release.db"))
    db.init_db()
    return db


def test_schema_is_versioned_and_clean_install_is_neutral(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db._conn()
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0] == "3"
    assert conn.execute("SELECT COUNT(*) FROM groups_").fetchone()[0] == 0
    assert conn.execute(
        "SELECT value FROM settings WHERE key='public_defaults_v1'"
    ).fetchone()[0] == "1"


def test_upgrade_adds_release_tables_without_losing_lineup(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db._conn()
    sid = conn.execute(
        "INSERT INTO sources(name,type,is_active) VALUES('Existing','m3u',1)"
    ).lastrowid
    conn.execute("INSERT INTO groups_(id,name) VALUES('existing-group','Existing')")
    conn.execute(
        "INSERT INTO channels(id,source_id,name,group_id,url) VALUES('existing-channel',?,'Existing','existing-group','http://example.invalid')",
        (sid,),
    )
    conn.execute("DROP TABLE teamarr_category_ids")
    conn.execute("DROP TABLE teamarr_stream_ids")
    conn.execute("DROP TABLE lineup_snapshots")
    conn.execute("DROP TABLE action_history")
    conn.execute("DROP TABLE schema_meta")
    conn.commit()

    db.init_db()
    assert db.get_channel("existing-channel")["name"] == "Existing"
    assert conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0] == "3"
    for table in ("lineup_snapshots", "action_history"):
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()


def test_management_requires_admin_auth(monkeypatch, tmp_path):
    _fresh_db(monkeypatch, tmp_path)
    from app.main import app

    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/dashboard").status_code == 401
    token = base64.b64encode(b"admin:test-only-password").decode()
    assert client.get(
        "/api/dashboard", headers={"Authorization": f"Basic {token}"}
    ).status_code == 200
    assert client.post(
        "/api/does-not-exist",
        headers={"Authorization": f"Basic {token}",
                 "Origin": "https://attacker.example"},
    ).status_code == 403


def test_direct_feeds_require_independent_token(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "_FEED_TOKEN", "feed-secret")
    client = TestClient(main.app)
    assert client.get("/m3u").status_code == 401
    # There is no lineup in this unit test, but valid feed auth passes the
    # middleware and reaches the endpoint rather than returning 401.
    assert client.get("/m3u?token=feed-secret").status_code == 503


def test_error_redaction_covers_query_path_and_url_credentials():
    from app.main import _redact_text

    redacted = _redact_text(
        "https://user:pass@example.test/live/alice/secret/42.ts?token=abc&key=xyz"
    )
    assert "alice" not in redacted
    assert "secret" not in redacted
    assert "pass" not in redacted
    assert "abc" not in redacted
    assert "xyz" not in redacted


def test_hls_rewrite_uses_opaque_targets():
    from app.main import _resolve_stream_target, _rewrite_hls_playlist

    upstream = "https://provider.example/live/user/provider-password/playlist.m3u8"
    rewritten = _rewrite_hls_playlist(
        "#EXTM3U\n#EXTINF:10,\nsegment.ts?token=upstream-secret\n",
        upstream,
        "channel-a",
    )
    assert "provider-password" not in rewritten
    assert "upstream-secret" not in rewritten
    key = rewritten.split("?k=", 1)[1].strip()
    assert _resolve_stream_target("channel-a", key).endswith(
        "segment.ts?token=upstream-secret"
    )


def test_teamarr_uses_numeric_stable_ids_and_returns_epg(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db._conn()
    sid = conn.execute(
        "INSERT INTO sources(name,type,is_active) VALUES('Example','m3u',1)"
    ).lastrowid
    conn.execute(
        "INSERT INTO groups_(id,name,teamarr,sort_order) VALUES('group-a','News',1,0)"
    )
    conn.execute(
        """INSERT INTO channels
           (id,source_id,stream_id,name,source_name,source_group,group_id,tvg_id,url,enabled,sort_order)
           VALUES('channel-a',?,99,'Example News','Example News','News','group-a',
                  'example.news','http://example.invalid/live',1,0)""", (sid,)
    )
    conn.execute("UPDATE settings SET value='1' WHERE key='teamarr_enabled'")
    conn.execute("UPDATE settings SET value='viewer' WHERE key='teamarr_username'")
    conn.execute("UPDATE settings SET value='secret' WHERE key='teamarr_password'")
    conn.commit()

    import app.main as main
    now = datetime.now(timezone.utc)
    main._guide_cache = {"channels": [{
        "id": "channel-a",
        "programmes": [(now.isoformat(), (now + timedelta(hours=1)).isoformat(),
                        "Evening News", "", "", "Headlines")],
    }]}
    client = TestClient(main.app)
    params = {"username": "viewer", "password": "secret"}
    categories = client.get("/player_api.php", params={
        **params, "action": "get_live_categories"
    }).json()
    streams = client.get("/player_api.php", params={
        **params, "action": "get_live_streams"
    }).json()
    assert categories[0]["category_id"].isdigit()
    assert isinstance(streams[0]["stream_id"], int)
    assert streams[0]["num"] == 1
    epg = client.get("/player_api.php", params={
        **params, "action": "get_short_epg", "stream_id": streams[0]["stream_id"]
    }).json()["epg_listings"]
    assert base64.b64decode(epg[0]["title"]).decode() == "Evening News"
    simple = client.get("/player_api.php", params={
        **params, "action": "get_simple_data_table", "stream_id": streams[0]["stream_id"]
    }).json()["epg_listings"]
    assert simple == epg
    for action in ("get_vod_categories", "get_vod_streams",
                   "get_series_categories", "get_series"):
        assert client.get("/player_api.php", params={**params, "action": action}).json() == []
    assert client.get("/player_api.php", params={
        "username": "viewer", "password": "wrong"
    }).status_code == 403
    account = client.get("/player_api.php", params=params).json()
    assert account["user_info"]["status"] == "Active"
    assert account["server_info"]["timezone"] == "UTC"
    assert client.get("/teamarr.xml").status_code == 403


def test_backup_restore_preserves_player_and_teamarr_ids(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db._conn()
    sid = conn.execute(
        "INSERT INTO sources(name,type,is_active) VALUES('Example','m3u',1)"
    ).lastrowid
    conn.execute("INSERT INTO groups_(id,name) VALUES('g','Group')")
    conn.execute(
        """INSERT INTO channels(id,source_id,name,group_id,url)
           VALUES('c',?,'Channel','g','http://example.invalid')""", (sid,)
    )
    conn.commit()
    db.assign_export_chnos(["c"])
    stream_id = db.get_teamarr_stream_id("c")
    category_id = db.get_teamarr_category_id("g")
    backup = db.export_all_data()
    conn.execute("UPDATE chno_map SET chno=99 WHERE channel_id='c'")
    conn.execute("DELETE FROM teamarr_stream_ids")
    conn.commit()

    db.import_all_data(backup)
    assert db.assign_export_chnos(["c"])["c"] == 1
    assert db.get_teamarr_stream_id("c") == stream_id
    assert db.get_teamarr_category_id("g") == category_id


def test_diagnostics_bundle_omits_credentials(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db._conn()
    conn.execute(
        """INSERT INTO sources
           (name,type,xc_server,xc_username,xc_password,epg_url,is_active)
           VALUES('Private Provider','xc','https://private.example','alice',
                  'provider-password','https://guide.example?token=guide-secret',1)"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings(key,value) VALUES('ntfy_token','notify-secret')"
    )
    conn.commit()
    from app.main import app

    client = TestClient(app)
    response = client.get(
        "/api/system/diagnostics-bundle",
        auth=("admin", "test-only-password"),
    )
    assert response.status_code == 200
    body = response.text
    for private in ("Private Provider", "private.example", "alice",
                    "provider-password", "guide-secret", "notify-secret"):
        assert private not in body
