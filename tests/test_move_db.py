import sqlite3

import pytest

from app import db
from scripts import move_db


def _make_db(path):
    c = db.connect(str(path))
    db.init_db(c)
    for i in range(4):
        c.execute(
            "INSERT INTO entries (created_at, local_date, text) VALUES (?, ?, ?)",
            (f"2026-10-0{i + 1}T08:00:00+00:00", f"2026-10-0{i + 1}", f"fake entry {i}"),
        )
    c.commit()
    c.close()


def test_move_db_verifies_counts_and_keeps_source(tmp_path):
    src = tmp_path / "vault.db"
    _make_db(src)
    before = src.read_bytes()
    dst = tmp_path / "data" / "vault.db"
    counts = move_db.move(src, dst)
    assert counts["entries"] == 4
    assert src.exists()
    assert src.read_bytes() == before
    c = sqlite3.connect(str(dst))
    assert c.execute("SELECT count(*) FROM entries").fetchone()[0] == 4
    assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    c.close()
    assert [p.name for p in dst.parent.iterdir()] == ["vault.db"]


def test_move_refuses_overwrite(tmp_path, capsys):
    src = tmp_path / "vault.db"
    _make_db(src)
    dst = tmp_path / "taken.db"
    dst.write_bytes(b"precious")
    with pytest.raises(move_db.MoveError, match="destination_exists"):
        move_db.move(src, dst)
    assert dst.read_bytes() == b"precious"
    assert move_db.main([str(src), str(dst)]) == 1
    assert "destination_exists" in capsys.readouterr().out


def test_move_missing_source(tmp_path):
    with pytest.raises(move_db.MoveError, match="source_missing"):
        move_db.move(tmp_path / "nope.db", tmp_path / "out.db")
    assert not (tmp_path / "out.db").exists()


def test_move_cli_prints_counts_only(tmp_path, capsys):
    src = tmp_path / "vault.db"
    _make_db(src)
    assert move_db.main([str(src), str(tmp_path / "d" / "vault.db")]) == 0
    out = capsys.readouterr().out
    assert "entries: 4" in out
    assert "fake entry" not in out
