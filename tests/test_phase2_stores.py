import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import config, db, focus, memory, reflections

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_init_db_creates_phase2_tables_idempotently(conn):
    db.init_db(conn)
    db.init_db(conn)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reflections", "focus_areas", "ai_memory", "reflection_jobs", "entries", "settings"} <= names


def test_memory_add_list_delete(conn):
    a = memory.add_note(conn, kind="note", text="first", now=NOW)
    b = memory.add_note(conn, kind="correction", text="second", reflection_id=None,
                        claim_id="c1", now=NOW + timedelta(minutes=1))
    rows = memory.list_active(conn)
    assert [r["id"] for r in rows] == [b, a]
    assert rows[0]["claim_id"] == "c1"
    assert rows[0]["created_at"] == "2026-11-01T12:01:00Z"
    assert memory.delete(conn, a, NOW) is True
    assert [r["id"] for r in memory.list_active(conn)] == [b]
    assert memory.delete(conn, 9999, NOW) is False
    assert memory.delete(conn, a, NOW) is False


def test_memory_list_caps_at_limit(conn):
    for i in range(45):
        memory.add_note(conn, kind="note", text=f"n{i}", now=NOW + timedelta(minutes=i))
    rows = memory.list_active(conn)
    assert len(rows) == 40
    assert rows[0]["text"] == "n44"
    assert len(memory.list_active(conn, limit=5)) == 5


def test_memory_rejects_unknown_kind(conn):
    with pytest.raises(sqlite3.IntegrityError):
        memory.add_note(conn, kind="bogus", text="x", now=NOW)


def test_focus_status_transitions(conn):
    fid = focus.add(conn, "sleep earlier", None, NOW)
    assert focus.list_all(conn)[0]["status"] == "active"
    later = NOW + timedelta(days=1)
    assert focus.set_status(conn, fid, "done", later) is True
    row = focus.list_all(conn)[0]
    assert row["status"] == "done" and row["completed_at"] == "2026-11-02T12:00:00Z"
    assert focus.set_status(conn, fid, "active", later) is True
    row = focus.list_all(conn)[0]
    assert row["status"] == "active" and row["completed_at"] is None
    assert focus.set_status(conn, fid, "failing", later) is True
    assert focus.list_all(conn)[0]["completed_at"] is None
    with pytest.raises(ValueError):
        focus.set_status(conn, fid, "bogus", later)
    assert focus.set_status(conn, 9999, "done", later) is False


def _save(conn, period_key="2026-W44", end="2026-11-01", content=None, now=NOW):
    return reflections.save(
        conn, kind="weekly", period_key=period_key, period_start="2026-10-26",
        period_end=end, model="m", content=content or {"wins": []}, now=now,
    )


def test_reflection_versions(conn):
    first = _save(conn)
    second = _save(conn, content={"wins": ["x"]})
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM reflections")}
    assert rows[first]["version"] == 1 and rows[first]["is_latest"] == 0
    assert rows[second]["version"] == 2 and rows[second]["is_latest"] == 1
    assert reflections.content(reflections.get(conn, second)) == {"wins": ["x"]}
    assert reflections.get(conn, 9999) is None


def test_latest_returns_newest_period(conn):
    _save(conn, "2026-W43", "2026-10-25")
    newest = _save(conn, "2026-W44", "2026-11-01")
    _save(conn, "2026-W42", "2026-10-18")
    assert reflections.latest(conn, "weekly")["id"] == newest
    assert reflections.latest(conn, "monthly") is None


def test_job_unique_per_period(conn):
    sql = ("INSERT INTO reflection_jobs(kind, period_key, status, created_at)"
           " VALUES ('weekly', '2026-W44', 'pending', '2026-11-01T12:00:00Z')")
    conn.execute(sql)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_nvidia_settings_default_base_url_and_missing_key(monkeypatch):
    for k in ("NVIDIA_API_KEY", "NVIDIA_BASE_URL", "NVIDIA_MODEL"):
        monkeypatch.delenv(k, raising=False)
    s = config.get_nvidia_settings()
    assert s.api_key is None
    assert s.base_url == "https://integrate.api.nvidia.com/v1"
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key")
    monkeypatch.setenv("NVIDIA_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("NVIDIA_MODEL", "fake/model")
    s = config.get_nvidia_settings()
    assert (s.api_key, s.base_url, s.model) == ("fake-key", "https://example.test/v1", "fake/model")
    monkeypatch.setenv("NVIDIA_API_KEY", "  ")
    assert config.get_nvidia_settings().api_key is None


def test_nvidia_settings_repr_hides_key(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "fake-key-123")
    s = config.get_nvidia_settings()
    assert s.api_key == "fake-key-123"
    assert "fake-key-123" not in repr(s)
    assert "fake-key-123" not in str(s)
    assert "fake-key-123" not in f"{s}"


def test_list_active_puts_corrections_first_and_caps_them(conn):
    for i in range(30):
        memory.add_note(conn, kind="correction", text=f"c{i}", now=NOW + timedelta(minutes=i))
    for i in range(50):
        memory.add_note(conn, kind="note", text=f"n{i}", now=NOW + timedelta(hours=1, minutes=i))
    rows = memory.list_active(conn)
    assert len(rows) == memory.PROMPT_LIMIT == 40
    kinds = [r["kind"] for r in rows]
    assert kinds == ["correction"] * 25 + ["note"] * 15
    assert rows[0]["text"] == "c29" and rows[24]["text"] == "c5"
    assert rows[25]["text"] == "n49" and rows[-1]["text"] == "n35"
    # Few corrections: notes fill the remaining slots; the page query is plain newest first.
    assert memory.list_all_active(conn, 3)[0]["text"] == "n49"


def test_old_corrections_not_pushed_out_by_newer_notes(conn):
    old = memory.add_note(conn, kind="correction", text="old correction", now=NOW)
    for i in range(45):
        memory.add_note(conn, kind="note", text=f"n{i}", now=NOW + timedelta(minutes=i + 1))
    rows = memory.list_active(conn)
    assert rows[0]["id"] == old and len(rows) == 40
