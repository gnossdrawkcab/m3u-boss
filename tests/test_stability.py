"""Regression tests for lineup stability guarantees."""

import sqlite3


def _fresh_db(monkeypatch, tmp_path):
    import app.db as db

    old = getattr(db._local, "conn", None)
    if old is not None:
        old.close()
        del db._local.conn
    path = tmp_path / "stability.db"
    monkeypatch.setenv("M3U_BOSS_DB", str(path))
    db.init_db()
    return db


def test_provider_disappear_reappear_preserves_identity_and_placement(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    conn = db._conn()
    sid = conn.execute(
        "INSERT INTO sources(name,type,is_active) VALUES('Test','m3u',1)"
    ).lastrowid
    gid = "stable-group"
    cid = "stable-channel"
    conn.execute(
        "INSERT INTO groups_(id,name,is_custom,sort_order) VALUES(?,?,1,0)",
        (gid, "My Lineup"))
    conn.execute(
        """INSERT INTO channels
           (id,source_id,stream_id,name,source_name,source_group,group_id,tvg_id,
            original_tvg_id,url,sort_order,placement_locked,name_locked)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, sid, 42, "My Custom Name", "Provider Name", "Provider Group", gid,
         "guide.id", "guide.id", "http://old/42", 7, 1, 1))
    conn.execute("INSERT INTO chno_map(channel_id,chno) VALUES(?,?)", (cid, 321))
    conn.commit()

    gone = db.sync_channels(sid, [])
    assert gone == {"added": 0, "updated": 0, "removed": 1, "revived": 0}
    assert conn.execute("SELECT COUNT(*) FROM channel_tombstones").fetchone()[0] == 1
    db.assign_export_chnos([])
    assert conn.execute("SELECT chno FROM chno_map WHERE channel_id=?", (cid,)).fetchone()[0] == 321

    back = db.sync_channels(sid, [{
        "id": "provider-new-id", "source_id": sid, "stream_id": 42,
        "name": "Provider Renamed", "source_name": "Provider Renamed",
        "source_group": "Provider Group", "tvg_id": "guide.id",
        "url": "http://new/42", "source_order": 999,
    }])
    assert back == {"added": 0, "updated": 0, "removed": 0, "revived": 1}
    channel = db.get_channel(cid)
    assert channel["group_id"] == gid
    assert channel["sort_order"] == 7
    assert channel["placement_locked"] == 1
    assert channel["name"] == "My Custom Name"
    assert channel["url"] == "http://new/42"
    assert conn.execute("SELECT chno FROM chno_map WHERE channel_id=?", (cid,)).fetchone()[0] == 321


def test_rule_preview_leaves_unmatched_channels_where_they_are(monkeypatch):
    import app.rules as rules

    group = {"id": "target", "name": "24/7 TV", "sort_order": 0, "is_custom": 0}
    channels = [
        {"id": "match", "name": "Show", "source_group": "24/7 TV", "group_id": "target"},
        {"id": "other", "name": "Local", "source_group": "Ungrouped", "group_id": "curated"},
    ]
    monkeypatch.setattr(rules, "get_active_source_ids", lambda: [1])
    monkeypatch.setattr(rules, "get_all_channels_raw", lambda source_id=None: channels)
    monkeypatch.setattr(rules, "list_groups", lambda: [group])
    monkeypatch.setattr(rules, "get_all_rules_by_group", lambda: {
        "target": [{"field": "source_group", "pattern": "24/7 TV", "match_type": "exact"}]
    })
    monkeypatch.setattr(rules, "get_group_max_sort_order", lambda gid: 0)

    result = rules.preview_rules()
    assert result["moves"] == 0
    assert result["unchanged"] == 2


def test_connection_pool_replaces_a_poisoned_handle(monkeypatch, tmp_path):
    db = _fresh_db(monkeypatch, tmp_path)
    first = db._conn()
    first.close()  # Models a handle invalidated by a transient storage error.
    recovered = db._conn()
    assert recovered is not first
    assert recovered.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] > 0
