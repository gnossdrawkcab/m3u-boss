"""Safety Center: snapshots, import previews, validation, and API contracts."""

from fastapi.testclient import TestClient


def _fresh_db(monkeypatch, tmp_path):
    import app.db as db

    old = getattr(db._local, "conn", None)
    if old is not None:
        old.close()
        del db._local.conn
    monkeypatch.setenv("M3U_BOSS_DB", str(tmp_path / "safety.db"))
    db.init_db()
    return db


def _lineup(db):
    conn = db.connection()
    sid = conn.execute(
        """INSERT INTO sources(name,type,xc_password,is_active)
           VALUES('Private Source','m3u','never-snapshot-this',1)"""
    ).lastrowid
    conn.execute("INSERT INTO groups_(id,name,sort_order) VALUES('sports','Sports',0)")
    conn.execute("INSERT INTO groups_(id,name,sort_order) VALUES('movies','Movies',1)")
    conn.execute(
        """INSERT INTO channels
           (id,source_id,name,source_name,source_group,group_id,url,sort_order,placement_locked)
           VALUES('espn',?,'ESPN','ESPN','Sports','sports','http://example.invalid/espn',0,1)""",
        (sid,),
    )
    conn.commit()
    return sid


def test_named_snapshot_diff_restore_and_secret_exclusion(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    _lineup(db)
    from app import history

    snap = history.create_snapshot("Known good", "manual")
    row = db.connection().execute(
        "SELECT payload,checksum FROM lineup_snapshots WHERE id=?", (snap["id"],)
    ).fetchone()
    decoded = history._decode(row["payload"], row["checksum"])
    assert "sources" not in decoded["tables"]
    assert "settings" not in decoded["tables"]
    assert "never-snapshot-this" not in str(decoded)
    assert "url" not in decoded["tables"]["channels"][0]
    assert "backup_urls" not in decoded["tables"]["channels"][0]

    db.connection().execute(
        "UPDATE channels SET group_id='movies',sort_order=9,name='ESPN Changed' WHERE id='espn'"
    )
    db.connection().commit()
    diff = history.snapshot_diff(snap["id"])
    assert diff["channels"]["moved"] == 1
    assert diff["channels"]["changed"] == 1

    history.restore_snapshot(snap["id"])
    channel = db.get_channel("espn")
    assert channel["group_id"] == "sports"
    assert channel["name"] == "ESPN"
    assert channel["placement_locked"] == 1


def test_snapshot_restores_deleted_station_from_private_tombstone(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    _lineup(db)
    from app import history

    snap = history.create_snapshot("Before delete")
    db.delete_channel("espn")
    assert db.get_channel("espn") is None
    tomb = db.connection().execute(
        "SELECT row_json FROM channel_tombstones WHERE channel_id='espn'"
    ).fetchone()
    assert "http://example.invalid/espn" in tomb["row_json"]
    history.restore_snapshot(snap["id"])
    restored = db.get_channel("espn")
    assert restored["url"] == "http://example.invalid/espn"
    assert restored["placement_locked"] == 1


def test_undo_restores_latest_automatic_safety_point(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    _lineup(db)
    from app import history

    snap = history.create_snapshot("Before bulk move", "automatic")
    history.record_action("POST /api/channels/bulk-move", "test", snap["id"])
    db.connection().execute("UPDATE channels SET group_id='movies' WHERE id='espn'")
    db.connection().commit()
    result = history.undo_latest()
    assert result["restored"]["id"] == snap["id"]
    assert db.get_channel("espn")["group_id"] == "sports"


def test_import_preview_flags_locked_removal_without_urls(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    sid = _lineup(db)
    from app.release_features import _preview_sync

    preview = _preview_sync(sid, [])
    assert preview["removed"] == 1
    assert preview["locked_removals"] == 1
    assert preview["requires_review"] is True
    assert "url" not in preview["sample"]["removed"][0]


def test_backup_validation_catches_orphans_and_accepts_clean_backup(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    _lineup(db)
    from app.release_features import _validate_backup

    clean = _validate_backup(db.export_all_data())
    assert clean["valid"] is True
    broken = db.export_all_data()
    broken["channels"][0]["source_id"] = 99999
    result = _validate_backup(broken)
    assert result["valid"] is False
    assert "missing sources" in result["errors"][0]


def test_epg_quality_finds_empty_generic_and_blank_rows(tmp_path):
    from app.release_features import _epg_quality

    (tmp_path / "organized.xml").write_text(
        """<?xml version='1.0'?><tv>
        <channel id='good'><display-name>Good</display-name></channel>
        <channel id='generic'><display-name>Generic</display-name></channel>
        <channel id='empty'><display-name>Empty</display-name></channel>
        <programme channel='good' start='20260722120000 +0000' stop='20260722130000 +0000'><title>News at Noon</title></programme>
        <programme channel='generic' start='20260722120000 +0000' stop='20260722130000 +0000'><title>Movie</title></programme>
        <programme channel='generic' start='20260722130000 +0000' stop='20260722140000 +0000'><title></title></programme>
        </tv>""",
        encoding="utf-8",
    )
    result = _epg_quality(tmp_path)
    assert result["status"] == "ok"
    assert result["empty_channels"] == 1
    assert result["generic_title_channels"] == 1
    assert result["blank_title_channels"] == 1


def test_safety_api_and_teamarr_matrix(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    _lineup(db)
    from app.main import app

    client = TestClient(app)
    auth = ("admin", "test-only-password")
    overview = client.get("/api/safety/overview", auth=auth)
    assert overview.status_code == 200
    assert overview.json()["stats"]["locked"] == 1
    snapshot = client.post(
        "/api/history/snapshots", auth=auth, json={"label": "API snapshot"}
    )
    assert snapshot.status_code == 200
    assert client.get("/api/history", auth=auth).json()["snapshots"]
    locked = client.post(
        "/api/channels/bulk-lock", auth=auth,
        json={"channel_ids": ["espn"], "locked": False},
    )
    assert locked.status_code == 200
    assert db.get_channel("espn")["placement_locked"] == 0
    assert client.get("/api/history", auth=auth).json()["actions"][0]["snapshot_id"]
    matrix = client.get("/api/teamarr/compatibility", auth=auth).json()
    assert "get_short_epg" in matrix["contract"]["actions"]
    migrations = client.get("/api/system/migrations", auth=auth).json()
    assert migrations == {
        "schema_version": "3", "current": "3", "healthy": True, "missing_tables": []
    }
    broken = db.export_all_data()
    broken["channels"][0]["source_id"] = 99999
    rejected = client.post("/api/restore", auth=auth, json=broken)
    assert rejected.status_code == 400
    assert db.get_channel("espn") is not None
