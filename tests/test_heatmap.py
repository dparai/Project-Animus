"""Writing calendar: the grid, the streaks, and the day filter on the Entries page."""
from datetime import date, datetime, timezone

import pytest

from app import db, entries, heatmap
from app.main import create_app
from tests.auth_helpers import login_client

TODAY = date(2026, 11, 16)  # a Monday


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "heat.db")
    conn = db.connect(path)
    db.init_db(conn)
    conn.close()
    return path


def _add(db_path, day, text="x", mood=None, hour=20):
    conn = db.connect(db_path)
    try:
        return entries.create_entry(
            conn, text=text, mood=mood, energy=None,
            now=datetime.fromisoformat(f"{day}T{hour:02d}:00:00").replace(tzinfo=timezone.utc), tz="UTC",
        )
    finally:
        conn.close()


def _build(db_path, today=TODAY):
    conn = db.connect(db_path)
    try:
        return heatmap.build(conn, today)
    finally:
        conn.close()


def _cell(heat, day):
    return next(c for col in heat["columns"] for c in col["cells"] if c["date"] == day)


def test_empty_journal_has_a_full_grid_and_zero_streaks(db_path):
    heat = _build(db_path)
    assert len(heat["columns"]) == heatmap.WEEKS
    assert all(len(col["cells"]) == 7 for col in heat["columns"])
    assert (heat["days_written"], heat["streak"], heat["longest"]) == (0, 0, 0)


def test_grid_ends_in_this_week_and_marks_future_days(db_path):
    heat = _build(db_path)
    last = heat["columns"][-1]["cells"]
    assert last[0]["date"] == "2026-11-15"  # weeks start on Sunday
    assert [c["future"] for c in last] == [False, False, True, True, True, True, True]
    assert heat["columns"][0]["cells"][0]["date"] == "2025-11-16"


def test_counts_levels_and_latest_mood(db_path):
    _add(db_path, "2026-11-10", mood="low", hour=9)
    _add(db_path, "2026-11-10", mood="good", hour=21)
    for hour in range(5):
        _add(db_path, "2026-11-12", hour=hour + 1)
    heat = _build(db_path)
    two = _cell(heat, "2026-11-10")
    assert (two["count"], two["level"], two["mood"]) == (2, 2, "good")
    assert "2 entries, good" in two["label"]
    five = _cell(heat, "2026-11-12")
    assert (five["count"], five["level"], five["mood"]) == (5, heatmap.MAX_LEVEL, None)
    assert _cell(heat, "2026-11-11")["label"].endswith("no entry")
    assert (heat["days_written"], heat["entries"]) == (2, 7)


def test_streak_counts_back_from_today_or_yesterday(db_path):
    for day in ("2026-11-13", "2026-11-14", "2026-11-15"):
        _add(db_path, day)
    for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"):
        _add(db_path, day)
    heat = _build(db_path)
    assert (heat["streak"], heat["longest"]) == (3, 5)  # today not written yet: the run still stands
    _add(db_path, "2026-11-16")
    assert _build(db_path)["streak"] == 4
    assert _build(db_path, date(2026, 11, 18))["streak"] == 0  # a missed day ends it


def test_month_labels_appear_once_per_month(db_path):
    labels = [col["month"] for col in _build(db_path)["columns"] if col["month"]]
    assert all(labels.count(name) == 1 for name in set(labels) - {"Nov"}) and labels.count("Nov") <= 2
    assert "Jan" in labels and "Jun" in labels


# --- Entries page ---

@pytest.fixture
def client(db_path):
    return login_client(create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False))


def test_entries_page_shows_the_calendar_and_links_written_days(client, db_path):
    _add(db_path, "2026-11-10", text="tuesday words", mood="okay")
    html = client.get("/entries").text
    assert 'id="heatmap"' in html
    assert 'href="/entries?day=2026-11-10"' in html and 'm-okay' in html


def test_day_filter_fragment_lists_only_that_day(client, db_path):
    _add(db_path, "2026-11-10", text="tuesday words")
    _add(db_path, "2026-11-11", text="wednesday words")
    r = client.get("/entries?day=2026-11-10", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "tuesday words" in r.text and "wednesday words" not in r.text
    assert "Tuesday 10 November 2026" in r.text and "Show all entries" in r.text
    assert 'id="heatmap"' not in r.text  # the fragment is the list only


def test_day_filter_full_page_marks_the_day(client, db_path):
    _add(db_path, "2026-11-10", text="tuesday words")
    html = client.get("/entries?day=2026-11-10").text
    assert 'aria-current="date"' in html and "tuesday words" in html


def test_day_with_no_entries_says_so(client):
    r = client.get("/entries?day=2026-11-09", headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Nothing was written that day." in r.text


@pytest.mark.parametrize("bad", ["2026-13-01", "today", "2026-11-1", "20261110", "2026-11-10'--"])
def test_bad_day_is_refused(client, bad):
    assert client.get("/entries", params={"day": bad}).status_code == 400


def test_calendar_needs_a_session(db_path):
    from tests.auth_helpers import anon_client
    r = anon_client(create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False)).get("/entries?day=2026-11-10")
    assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
