from datetime import datetime, timezone

import pytest

from app import entries

NOW = datetime(2026, 10, 31, 1, 32, 15, tzinfo=timezone.utc)
TZ = "America/Los_Angeles"


def _count(conn):
    return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]


def test_local_date_uses_timezone():
    assert entries.local_date("2026-10-31T01:32:15Z", TZ) == "2026-10-30"


def test_create_stamps_utc_and_local_day(conn):
    eid = entries.create_entry(conn, text="hello", mood="good", energy=7, now=NOW, tz=TZ)
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
    assert row["created_at"] == "2026-10-31T01:32:15Z"
    assert row["local_date"] == "2026-10-30"


def test_two_entries_same_day_allowed(conn):
    entries.create_entry(conn, text="a", mood=None, energy=None, now=NOW, tz=TZ)
    entries.create_entry(conn, text="b", mood=None, energy=None, now=NOW, tz=TZ)
    assert _count(conn) == 2


@pytest.mark.parametrize("text", ["", "   \n"])
def test_blank_text_rejected(conn, text):
    with pytest.raises(ValueError):
        entries.create_entry(conn, text=text, mood=None, energy=None, now=NOW, tz=TZ)
    assert _count(conn) == 0


def test_energy_and_mood_validated(conn):
    for mood, energy in (("good", 0), ("good", 11), ("happy", 5)):
        with pytest.raises(ValueError):
            entries.create_entry(conn, text="x", mood=mood, energy=energy, now=NOW, tz=TZ)
    entries.create_entry(conn, text="x", mood=None, energy=None, now=NOW, tz=TZ)
    assert _count(conn) == 1


def test_update_and_delete(conn):
    eid = entries.create_entry(conn, text="old", mood="okay", energy=3, now=NOW, tz=TZ)
    entries.update_entry(conn, eid, text="  new  ", mood=None, energy=9)
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
    assert (row["text"], row["mood"], row["energy_score"]) == ("new", None, 9)
    with pytest.raises(ValueError):
        entries.update_entry(conn, eid, text=" ", mood=None, energy=None)
    entries.delete_entry(conn, eid)
    assert _count(conn) == 0


def test_list_entries_newest_first(conn):
    first = entries.create_entry(conn, text="first", mood=None, energy=None, now=NOW, tz=TZ)
    later = datetime(2026, 11, 1, 5, 0, 0, tzinfo=timezone.utc)
    second = entries.create_entry(conn, text="second", mood=None, energy=None, now=later, tz=TZ)
    assert [r["id"] for r in entries.list_entries(conn)] == [second, first]


def test_get_entry_returns_row_or_none(conn):
    eid = entries.create_entry(conn, text="find me", mood=None, energy=None, now=NOW, tz=TZ)
    assert entries.get_entry(conn, eid)["text"] == "find me"
    assert entries.get_entry(conn, eid + 1) is None


def test_update_clears_approximate_flag_only_when_energy_changes(conn):
    eid = entries.create_entry(
        conn, text="imp", mood=None, energy=5, now=NOW, tz=TZ, energy_is_approximate=True
    )
    entries.update_entry(conn, eid, text="imp edited", mood=None, energy=5)
    assert entries.get_entry(conn, eid)["energy_is_approximate"] == 1
    entries.update_entry(conn, eid, text="imp edited", mood=None, energy=6)
    assert entries.get_entry(conn, eid)["energy_is_approximate"] == 0


def test_list_entries_before_cursor(conn):
    from datetime import datetime, timezone
    now = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
    ids = [entries.create_entry(conn, text=f"t{i}", mood=None, energy=None, now=now, tz="UTC")
           for i in range(5)]
    assert [r["id"] for r in entries.list_entries(conn)] == ids[::-1]
    page = entries.list_entries(conn, limit=2, before=("2026-10-01T10:00:00Z", ids[3]))
    assert [r["id"] for r in page] == [ids[2], ids[1]]
