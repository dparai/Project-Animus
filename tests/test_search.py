from datetime import date

import pytest

from app import search

TODAY = date(2026, 11, 1)


def add(conn, text, day, mood=None):
    conn.execute(
        "INSERT INTO entries(created_at, local_date, text, mood) VALUES (?, ?, ?, ?)",
        (f"{day}T12:00:00+00:00", day, text, mood),
    )
    conn.commit()


def texts(rows):
    return [r["text"] for r in rows]


def test_build_query_strips_operators(conn):
    q = search.build_query('NEAR("a" AND *) -b')
    assert q is None or isinstance(q, str)
    if q:
        conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?", (q,)).fetchall()
    q = search.build_query('"quoted" apples* AND pears -x')
    assert q == '"quoted" OR "apples" OR "pears"'
    conn.execute("SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?", (q,)).fetchall()


def test_build_query_none_for_punctuation_only():
    assert search.build_query('?!... *** "" --') is None
    assert search.build_query("") is None
    assert search.build_query("what is the") is None


def test_find_entries_ranks_matching_entry_first(conn):
    add(conn, "went to the market and bought apples", "2026-10-01")
    add(conn, "apples apples apples and more apples pie", "2026-10-02")
    add(conn, "nothing relevant here", "2026-10-03")
    rows = search.find_entries(conn, "apples pie", TODAY)
    assert rows[0]["text"].startswith("apples apples")
    assert len(rows) == 2


def test_find_entries_date_hint_last_month(conn):
    add(conn, "garden work in september", "2026-09-30")
    add(conn, "garden work in october", "2026-10-15")
    add(conn, "garden work in november", "2026-11-01")
    rows = search.find_entries(conn, "garden last month", TODAY)
    assert texts(rows)[0] == "garden work in october"


def test_find_entries_hint_filter_falls_back_when_empty(conn):
    add(conn, "garden work in september", "2026-09-30")
    rows = search.find_entries(conn, "garden last month", TODAY)
    assert texts(rows) == ["garden work in september"]


def test_find_entries_mood_hint(conn):
    add(conn, "walk by the river", "2026-10-01", mood="good")
    add(conn, "walk by the lake", "2026-10-02", mood="sad")
    rows = search.find_entries(conn, "walk when I felt sad", TODAY)
    assert texts(rows)[0] == "walk by the lake"


def test_find_entries_iso_date(conn):
    add(conn, "call with mom", "2026-03-04")
    add(conn, "call with dad", "2026-03-05")
    rows = search.find_entries(conn, "call on 2026-03-05", TODAY)
    assert texts(rows)[0] == "call with dad"


def test_find_entries_no_match_returns_empty(conn):
    add(conn, "call with mom", "2026-03-04")
    assert search.find_entries(conn, "volcano", TODAY) == []
    assert search.find_entries(conn, "???", TODAY) == []


def test_find_entries_limit(conn):
    for i in range(10):
        add(conn, f"hike number {i}", f"2026-10-{i + 1:02d}")
    assert len(search.find_entries(conn, "hike", TODAY, limit=3)) == 3
    assert len(search.find_entries(conn, "hike", TODAY)) == 8


def test_date_only_hint_returns_newest_in_range(conn):
    add(conn, "alpha", "2026-10-01")
    add(conn, "beta", "2026-10-20")
    add(conn, "gamma", "2026-09-20")
    rows = search.find_entries(conn, "what happened last month?", TODAY)
    assert texts(rows) == ["beta", "alpha"]


def test_mood_only_without_query_or_dates_returns_empty(conn):
    add(conn, "alpha", "2026-10-01", mood="sad")
    assert search.find_entries(conn, "sad", TODAY) == []


@pytest.mark.parametrize("q,expected", [
    ("yesterday", ("2026-10-31", "2026-10-31")),
    ("today", ("2026-11-01", "2026-11-01")),
    ("last week", ("2026-10-19", "2026-10-25")),
    ("this week", ("2026-10-26", "2026-11-01")),
    ("last month", ("2026-10-01", "2026-10-31")),
    ("this month", ("2026-11-01", "2026-11-01")),
    ("in March 2025", ("2025-03-01", "2025-03-31")),
    ("in march", ("2026-03-01", "2026-03-31")),
    ("in december", ("2025-12-01", "2025-12-31")),
    ("during 2024", ("2024-01-01", "2024-12-31")),
    ("on 2026-02-30", (None, None)),
])
def test_parse_hints_dates(q, expected):
    h = search.parse_hints(q, TODAY)
    assert (h.date_from, h.date_to) == expected


def test_parse_hints_moods():
    assert search.parse_hints("when was I sad or low", TODAY).moods == ("sad", "low")
    assert search.parse_hints("nothing", TODAY).moods == ()


def test_filtered_hits_topped_up_with_better_unfiltered(conn):
    add(conn, "fine, my knee hurt", "2026-10-01", mood="okay")
    add(conn, "knee knee knee knee pain all day", "2026-10-02")
    rows = search.find_entries(conn, "fine knee", TODAY)
    assert texts(rows) == ["fine, my knee hurt", "knee knee knee knee pain all day"]


def test_build_query_splits_underscore():
    assert search.build_query("foo_bar") == '"foo" OR "bar"'


def test_limit_is_clamped(conn):
    for i in range(3):
        add(conn, f"hike {i}", f"2026-10-0{i + 1}")
    assert len(search.find_entries(conn, "hike", TODAY, limit=0)) == 1
    assert len(search.find_entries(conn, "hike", TODAY, limit=-1)) == 1
    assert len(search.find_entries(conn, "hike", TODAY, limit=1000)) == 3


def test_query_token_cap():
    q = search.build_query(" ".join(f"word{i}" for i in range(5000)))
    assert q.count(" OR ") == 29
    assert q.startswith('"word0" OR "word1"')


@pytest.mark.parametrize("q,today,expected", [
    ("last month", date(2026, 1, 15), ("2025-12-01", "2025-12-31")),
    ("last week", date(2026, 11, 1), ("2026-10-19", "2026-10-25")),
    ("this week", date(2026, 11, 1), ("2026-10-26", "2026-11-01")),
    ("last week", date(2026, 11, 2), ("2026-10-26", "2026-11-01")),
    ("this week", date(2026, 11, 2), ("2026-11-02", "2026-11-02")),
])
def test_parse_hints_boundaries(q, today, expected):
    h = search.parse_hints(q, today)
    assert (h.date_from, h.date_to) == expected


@pytest.mark.parametrize("q,expected", [
    ("what may have helped", (None, None)),
    ("it may be hard to march on", (None, None)),
    ("what happened in May", ("2026-05-01", "2026-05-31")),
    ("in may", ("2026-05-01", "2026-05-31")),
    ("during march", ("2026-03-01", "2026-03-31")),
    ("end of may", ("2026-05-01", "2026-05-31")),
    ("may 2025 walks", ("2025-05-01", "2025-05-31")),
    ("March walks", ("2026-03-01", "2026-03-31")),
])
def test_ambiguous_month_words(q, expected):
    h = search.parse_hints(q, TODAY)
    assert (h.date_from, h.date_to) == expected
