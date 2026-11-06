import json
import logging
import os
import sqlite3
import stat
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import db
from scripts import offsite

NOW = datetime(2026, 11, 1, 4, 0, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
SECRET = "SECRETWORD-private-thought"

FAKE_RCLONE = """#!{python}
import json, os, shutil, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RCLONE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\\n")
if args[0] == "copyto":
    shutil.copy(args[1], os.path.join(os.environ["FAKE_RCLONE_UPLOADS"], os.path.basename(args[1])))
if os.environ.get("FAKE_RCLONE_FAIL") == args[0]:
    sys.exit(1)
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("fake rclone is a POSIX script")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "rclone"
    exe.write_text(FAKE_RCLONE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "animus-20261031-033000.db").write_bytes(b"x")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_RCLONE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setenv("FAKE_RCLONE_UPLOADS", str(uploads))
    monkeypatch.delenv("FAKE_RCLONE_FAIL", raising=False)
    monkeypatch.setenv("ANIMUS_DB_PATH", str(data / "vault.db"))
    monkeypatch.setenv("ANIMUS_BACKUP_DIR", str(backups))
    monkeypatch.delenv("ANIMUS_RCLONE_DOCS_REMOTE", raising=False)
    monkeypatch.delenv("ANIMUS_RCLONE_BACKUP_REMOTE", raising=False)
    return tmp_path


def _add(path, rows, tz=None):
    c = db.connect(str(path))
    db.init_db(c)
    if tz:
        c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('timezone', ?)", (tz,))
    for created_at, local_date, text, mood, energy, approx in rows:
        c.execute(
            "INSERT INTO entries (created_at, local_date, text, mood, energy_score, "
            "energy_is_approximate) VALUES (?, ?, ?, ?, ?, ?)",
            (created_at, local_date, text, mood, energy, approx),
        )
    c.commit()
    c.close()


def _calls(env):
    log = env / "calls.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _uploaded(env):
    return sorted(p.name for p in (env / "uploads").iterdir())


def _db(env):
    return env / "data" / "vault.db"


def test_html_header_order_escaping_and_timezone(env):
    _add(_db(env), [
        ("2026-10-31T01:00:00+00:00", "2026-10-30", "later <b>bold</b> & more\nsecond line\n\nnew para",
         "good", 7, 1),
        ("2026-10-30T17:15:00+00:00", "2026-10-30", "first entry", "okay", 6, 0),
        ("2026-10-29T16:05:00+00:00", "2026-10-29", "no meta", None, None, 0),
    ], tz="America/Los_Angeles")
    assert offsite.main(NOW) == 0
    page = (env / "uploads" / "Animus Journal 2026-10.html").read_text(encoding="utf-8")
    assert "<h1>Animus Journal October 2026</h1>" in page
    h_first = "<h2>Fri 30 Oct 2026, 10:15 · mood: okay · energy 6/10</h2>"
    h_later = "<h2>Fri 30 Oct 2026, 18:00 · mood: good · energy ~7/10</h2>"
    h_bare = "<h2>Thu 29 Oct 2026, 09:05</h2>"
    assert h_bare in page and h_first in page and h_later in page
    assert page.index(h_bare) < page.index(h_first) < page.index(h_later)
    assert "&lt;b&gt;bold&lt;/b&gt; &amp; more<br>\nsecond line</p>" in page
    assert "<p>new para</p>" in page
    assert "<b>bold</b>" not in page
    assert page.count("<section>") == 3


def test_timezone_changes_the_day_and_hour(env):
    _add(_db(env), [("2026-10-31T01:30:00+00:00", "2026-10-31", "tokyo", None, None, 0)],
         tz="Asia/Tokyo")
    assert offsite.main(NOW) == 0
    page = (env / "uploads" / "Animus Journal 2026-10.html").read_text(encoding="utf-8")
    assert "<h2>Sat 31 Oct 2026, 10:30</h2>" in page


def test_months_grouped_and_only_entries_exported(env):
    _add(_db(env), [
        ("2026-09-30T20:00:00+00:00", "2026-09-30", "sept", None, None, 0),
        ("2026-10-02T20:00:00+00:00", "2026-10-02", "oct", None, None, 0),
    ])
    assert offsite.main(NOW) == 0
    assert _uploaded(env) == ["Animus Journal 2026-09.html", "Animus Journal 2026-10.html"]
    sept = (env / "uploads" / "Animus Journal 2026-09.html").read_text(encoding="utf-8")
    assert "sept" in sept and "oct" not in sept


def test_first_run_all_months_then_only_two(env):
    _add(_db(env), [
        ("2026-08-10T20:00:00+00:00", "2026-08-10", "aug", None, None, 0),
        ("2026-09-10T20:00:00+00:00", "2026-09-10", "sep", None, None, 0),
        ("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0),
        ("2026-11-01T10:00:00+00:00", "2026-11-01", "nov", None, None, 0),
    ])
    assert offsite.main(NOW) == 0
    first = [c for c in _calls(env) if c[0] == "copyto"]
    assert len(first) == 4
    assert (env / "data" / "offsite_state.json").exists()
    (env / "calls.log").unlink()
    assert offsite.main(NOW) == 0
    second = [c[2] for c in _calls(env) if c[0] == "copyto"]
    assert second == ["gdrive:Animus Journal/Animus Journal 2026-10.html",
                      "gdrive:Animus Journal/Animus Journal 2026-11.html"]


def test_failed_first_run_does_not_mark_full_export(env, monkeypatch):
    _add(_db(env), [("2026-08-10T20:00:00+00:00", "2026-08-10", "aug", None, None, 0)])
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    assert offsite.main(NOW) == 1
    assert not (env / "data" / "offsite_state.json").exists()


def test_rclone_commands_exact(env, monkeypatch):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0)])
    monkeypatch.setenv("ANIMUS_RCLONE_DOCS_REMOTE", "docs:Folder/")
    monkeypatch.setenv("ANIMUS_RCLONE_BACKUP_REMOTE", "crypt:")
    assert offsite.main(NOW) == 0
    calls = _calls(env)
    assert len(calls) == 2
    copy = calls[0]
    assert copy[0] == "copyto" and copy[1].endswith("Animus Journal 2026-10.html")
    assert copy[2:] == ["docs:Folder/Animus Journal 2026-10.html", "--drive-import-formats", "html", "--drive-export-formats", "html"]
    assert calls[1] == ["sync", str(env / "backups"), "crypt:", "--include", "animus-*.db",
                        "--max-delete", "5", "--immutable"]


def test_default_remotes(env):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0)])
    assert offsite.main(NOW) == 0
    calls = _calls(env)
    assert calls[0][2] == "gdrive:Animus Journal/Animus Journal 2026-10.html"
    assert calls[1][2] == "gdrive-crypt:"


def test_empty_backup_dir_refuses_to_sync(env, caplog):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0)])
    for p in (env / "backups").iterdir():
        p.unlink()
    (env / "backups" / "notes.txt").write_text("not a backup")
    with caplog.at_level(logging.INFO):
        assert offsite.main(NOW) == 1
    assert not any(c[0] == "sync" for c in _calls(env))
    assert any(c[0] == "copyto" for c in _calls(env))  # docs half still ran
    assert "reason=backup_dir_empty" in caplog.text


def test_docs_failure_still_syncs_backups(env, monkeypatch, caplog):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0)])
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    with caplog.at_level(logging.INFO):
        assert offsite.main(NOW) == 1
    assert any(c[0] == "sync" for c in _calls(env))
    assert "reason=docs_failed" in caplog.text


def test_sync_failure_still_exports_docs(env, monkeypatch, caplog):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0)])
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "sync")
    with caplog.at_level(logging.INFO):
        assert offsite.main(NOW) == 1
    assert _uploaded(env) == ["Animus Journal 2026-10.html"]
    assert "docs_uploaded=1" in caplog.text
    assert "reason=backup_sync_failed" in caplog.text


def test_missing_db_is_docs_failed_and_backups_still_sync(env, caplog):
    with caplog.at_level(logging.INFO):
        assert offsite.main(NOW) == 1
    assert "reason=docs_failed" in caplog.text
    assert any(c[0] == "sync" for c in _calls(env))


def test_rclone_missing(env, monkeypatch, caplog):
    monkeypatch.setenv("PATH", str(env / "nowhere"))
    with caplog.at_level(logging.INFO):
        assert offsite.main(NOW) == 1
    assert "reason=rclone_missing" in caplog.text


def test_success_logs_counts_and_no_entry_text(env, caplog):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", SECRET, "good", 5, 0)])
    with caplog.at_level(logging.DEBUG):
        assert offsite.main(NOW) == 0
    assert "docs_uploaded=1" in caplog.text and "backups_synced" in caplog.text
    assert SECRET not in caplog.text


def test_failure_logs_no_entry_text(env, monkeypatch, caplog):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", SECRET, None, None, 0)])
    monkeypatch.setenv("FAKE_RCLONE_FAIL", "copyto")
    with caplog.at_level(logging.DEBUG):
        offsite.main(NOW)
    assert SECRET not in caplog.text


def test_temp_dir_removed(env, monkeypatch):
    seen = []
    real = offsite.tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        seen.append(d)
        return d

    monkeypatch.setattr(offsite.tempfile, "mkdtemp", spy)
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0)])
    assert offsite.main(NOW) == 0
    assert seen and not os.path.exists(seen[0])


def test_database_opened_read_only(env, monkeypatch):
    _add(_db(env), [("2026-10-10T20:00:00+00:00", "2026-10-10", "oct", None, None, 0)])
    conn = offsite.open_readonly(_db(env))
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO settings(key, value) VALUES ('x', 'y')")
    conn.close()
    uris = []
    real = sqlite3.connect

    def spy(target, *a, **k):
        uris.append((str(target), k.get("uri")))
        return real(target, *a, **k)

    monkeypatch.setattr(offsite.sqlite3, "connect", spy)
    offsite.read_months(_db(env))
    assert uris and all(u.endswith("?mode=ro") and uri is True for u, uri in uris)
