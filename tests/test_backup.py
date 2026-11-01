import logging
import os
import sqlite3
import stat
import threading
from datetime import datetime, timedelta

import pytest

from app import config, db
from scripts import backup

NOW = datetime(2026, 11, 1, 3, 30, 0)


def _make_db(path, rows=5):
    c = db.connect(str(path))
    db.init_db(c)
    for i in range(rows):
        c.execute(
            "INSERT INTO entries (created_at, local_date, text) VALUES (?, ?, ?)",
            (f"2026-10-0{i + 1}T08:00:00+00:00", f"2026-10-0{i + 1}", f"fake entry {i}"),
        )
    c.commit()
    c.close()


def _count(path, table="entries"):
    c = sqlite3.connect(str(path))
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def test_backup_is_valid_db_with_same_counts(tmp_path):
    src = tmp_path / "vault.db"
    _make_db(src, rows=5)
    out = backup.make_backup(src, tmp_path / "backups", NOW)
    assert out.name == "animus-20261101-033000.db"
    c = sqlite3.connect(str(out))
    assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    c.close()
    assert _count(out) == 5
    assert _count(out, "entries_fts") == 5


def test_backup_while_writing(tmp_path):
    src = tmp_path / "vault.db"
    _make_db(src, rows=50)
    stop = threading.Event()
    inserted = []

    def writer():
        c = db.connect(str(src))
        n = 0
        while not stop.is_set():
            c.execute(
                "INSERT INTO entries (created_at, local_date, text) VALUES (?, ?, ?)",
                ("2026-10-09T08:00:00+00:00", "2026-10-09", f"fake live {n}"),
            )
            c.commit()
            n += 1
        inserted.append(n)
        c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        outs = [backup.make_backup(src, tmp_path / "backups", NOW + timedelta(seconds=i)) for i in range(3)]
    finally:
        stop.set()
        t.join()
    for out in outs:
        c = sqlite3.connect(str(out))
        assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        c.close()
        assert _count(out) >= 50


def test_backup_mode_600_and_atomic_rename(tmp_path):
    src = tmp_path / "vault.db"
    _make_db(src)
    out_dir = tmp_path / "backups"
    out = backup.make_backup(src, out_dir, NOW)
    if os.name != "nt":
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        assert stat.S_IMODE(out_dir.stat().st_mode) == 0o700
    assert [p.name for p in out_dir.iterdir()] == [out.name]  # no temp file left behind


def _touch_backups(out_dir, stamps):
    out_dir.mkdir(exist_ok=True)
    paths = []
    for s in stamps:
        p = out_dir / f"animus-{s:%Y%m%d-%H%M%S}.db"
        p.write_bytes(b"x")
        paths.append(p)
    return paths


def test_prune_keeps_newest_and_daily_weekly_window(tmp_path):
    out_dir = tmp_path / "b"
    # 120 days of backups, two per day (03:30 and 15:00).
    stamps = []
    for d in range(120):
        day = NOW - timedelta(days=d)
        stamps.append(day)
        stamps.append(day.replace(hour=15, minute=0))
    paths = _touch_backups(out_dir, stamps)
    newest = max(paths)
    junk = out_dir / "notes.txt"
    junk.write_text("keep me")
    deleted = backup.prune(out_dir, now=NOW)
    left = {p for p in paths if p.exists()}
    assert newest in left
    assert junk.exists()
    assert set(deleted) == set(paths) - left
    # Daily window: the newest file of each of the 14 most recent days survives.
    recent_days = {(NOW - timedelta(days=d)).date() for d in range(14)}
    survivors_by_day = {}
    for p in left:
        survivors_by_day.setdefault(p.name[7:15], []).append(p)
    for day in recent_days:
        assert any(p.name.startswith(f"animus-{day:%Y%m%d}") for p in left)
    # Older than 14 days: only weekly keepers remain, at most 8 weeks.
    cutoff = f"animus-{(NOW - timedelta(days=13)):%Y%m%d}"
    older = [p for p in left if p.name < cutoff]
    assert 0 < len(older) <= 8 + 1
    # Never more than one file per old day.
    assert len({p.name[7:15] for p in older}) == len(older)


def test_prune_never_deletes_last_copy(tmp_path):
    out_dir = tmp_path / "b"
    (only,) = _touch_backups(out_dir, [NOW - timedelta(days=400)])
    assert backup.prune(out_dir, keep_daily=0, keep_weekly=0, now=NOW) == []
    assert only.exists()
    paths = _touch_backups(out_dir, [NOW - timedelta(days=400), NOW - timedelta(days=500)])
    backup.prune(out_dir, keep_daily=0, keep_weekly=0, now=NOW)
    assert max(paths).exists()


def test_backup_failure_exit_code_and_reason(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ANIMUS_DB_PATH", str(tmp_path / "missing.db"))
    monkeypatch.setenv("ANIMUS_BACKUP_DIR", str(tmp_path / "backups"))
    with caplog.at_level(logging.INFO, logger="animus.backup"):
        assert backup.main(now=NOW) == 1
    assert "reason=source_missing" in caplog.text
    with pytest.raises(backup.BackupError) as exc:
        backup.make_backup(tmp_path / "missing.db", tmp_path / "backups", NOW)
    assert exc.value.code == "source_missing"


def test_backup_corrupt_source_fails_and_leaves_no_file(tmp_path, monkeypatch, caplog):
    bad = tmp_path / "vault.db"
    bad.write_bytes(b"this is not a database" * 100)
    monkeypatch.setenv("ANIMUS_DB_PATH", str(bad))
    monkeypatch.setenv("ANIMUS_BACKUP_DIR", str(tmp_path / "backups"))
    with caplog.at_level(logging.INFO, logger="animus.backup"):
        assert backup.main(now=NOW) == 1
    assert "reason=backup_failed" in caplog.text
    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_main_success_logs_counts_only(tmp_path, monkeypatch, caplog):
    src = tmp_path / "vault.db"
    _make_db(src)
    monkeypatch.setenv("ANIMUS_DB_PATH", str(src))
    monkeypatch.setenv("ANIMUS_BACKUP_DIR", str(tmp_path / "backups"))
    with caplog.at_level(logging.INFO, logger="animus.backup"):
        assert backup.main(now=NOW) == 0
    assert "backup ok kept=1 pruned=0" in caplog.text
    assert "fake entry" not in caplog.text


def test_default_db_path_is_data_dir(monkeypatch):
    monkeypatch.delenv("ANIMUS_DB_PATH", raising=False)
    path = config.get_db_path()
    assert path == str(config.APP_DIR / "data" / "vault.db")
    assert os.path.isabs(path)
    monkeypatch.setenv("ANIMUS_DB_PATH", "/somewhere/else.db")
    assert config.get_db_path() == "/somewhere/else.db"


def test_two_backups_in_one_second_do_not_overwrite(tmp_path):
    src = tmp_path / "vault.db"
    _make_db(src, rows=3)
    out_dir = tmp_path / "backups"
    first = backup.make_backup(src, out_dir, NOW)
    first_bytes = first.read_bytes()
    second = backup.make_backup(src, out_dir, NOW)
    assert first != second and first.exists() and second.exists()
    assert first.read_bytes() == first_bytes
    assert second.name == "animus-20261101-033000-1.db"
    assert sorted(p.name for p in out_dir.iterdir()) == sorted([first.name, second.name])
    assert _count(second) == 3


def test_backup_fsyncs_file_before_rename_and_directory_after(tmp_path, monkeypatch):
    src = tmp_path / "vault.db"
    _make_db(src)
    out_dir = tmp_path / "backups"
    events = []
    monkeypatch.setattr(backup, "_fsync_path", lambda path, flags=os.O_RDONLY: events.append(("fsync", str(path))))
    real_link = os.link
    monkeypatch.setattr(backup.os, "link", lambda a, b: (events.append(("link", str(b))), real_link(a, b))[1])
    monkeypatch.setattr(backup, "_fsync_dir", lambda path: events.append(("fsync_dir", str(path))))
    backup.make_backup(src, out_dir, NOW)
    assert [e[0] for e in events] == ["fsync", "link", "fsync_dir"]
    assert events[0][1].endswith(".tmp")
    assert events[2][1] == str(out_dir)


def test_prune_counts_windows_from_now(tmp_path):
    out_dir = tmp_path / "b"
    old = _touch_backups(out_dir, [NOW - timedelta(days=60), NOW - timedelta(days=61)])
    newest = _touch_backups(out_dir, [NOW - timedelta(days=1)])[0]
    backup.prune(out_dir, keep_daily=14, keep_weekly=4, now=NOW)
    assert newest.exists()
    assert not any(p.exists() for p in old)  # 8+ weeks before now: outside both windows


def test_prune_handles_counter_suffix_names(tmp_path):
    out_dir = tmp_path / "b"
    out_dir.mkdir()
    a = out_dir / "animus-20261101-033000.db"
    b = out_dir / "animus-20261101-033000-1.db"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    backup.prune(out_dir, now=NOW)
    assert b.exists() and not a.exists()  # one per day: the later counter wins
