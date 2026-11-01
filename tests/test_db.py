import sqlite3

import pytest

from app import db


def _insert(conn, text="walked to the lake", mood="good", energy=5):
    cur = conn.execute(
        "INSERT INTO entries(created_at, local_date, text, mood, energy_score)"
        " VALUES ('2026-10-31T01:32:15Z', '2026-10-30', ?, ?, ?)",
        (text, mood, energy),
    )
    conn.commit()
    return cur.lastrowid


def _match(conn, word):
    return conn.execute(
        "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?", (word,)
    ).fetchall()


def test_connect_sets_busy_timeout(conn):
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000


def test_init_db_is_idempotent(conn):
    db.init_db(conn)
    db.init_db(conn)


def test_fts_finds_inserted_text(conn):
    _insert(conn)
    assert len(_match(conn, "lake")) == 1


def test_fts_follows_update_and_delete(conn):
    eid = _insert(conn)
    conn.execute("UPDATE entries SET text = 'sat by the river' WHERE id = ?", (eid,))
    conn.commit()
    assert _match(conn, "lake") == []
    assert len(_match(conn, "river")) == 1
    conn.execute("DELETE FROM entries WHERE id = ?", (eid,))
    conn.commit()
    assert _match(conn, "river") == []


def test_check_constraints(conn):
    for mood, energy in (("happy", 5), ("good", 0), ("good", 11)):
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, mood=mood, energy=energy)


def test_init_db_idempotent_with_chat_tables(conn):
    db.init_db(conn)
    db.init_db(conn)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"conversations", "messages"} <= tables
    conv_cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
    assert conv_cols == {"id", "started_at", "last_message_at", "ended_at", "summarized_at",
                         "summary_attempts", "last_attempt_at"}
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    assert msg_cols == {"id", "conversation_id", "role", "text", "cited_entry_ids", "created_at"}
    indexes = {r[1] for r in conn.execute("PRAGMA index_list(messages)")}
    assert "messages_conversation" in indexes
    conn.execute("INSERT INTO conversations(started_at, last_message_at) VALUES ('t', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO messages(conversation_id, role, text, created_at) VALUES (1, 'system', 'x', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO messages(conversation_id, role, text, created_at) VALUES (99, 'user', 'x', 't')")
    conn.execute("INSERT INTO messages(conversation_id, role, text, created_at) VALUES (1, 'user', 'x', 't')")
    assert conn.execute("SELECT cited_entry_ids FROM messages").fetchone()[0] == "[]"
