from datetime import date

from app import stats

TZ = "America/Los_Angeles"


def add(conn, local_date, *, mood=None, energy=None, approx=0):
    conn.execute(
        "INSERT INTO entries(created_at, local_date, text, mood, energy_score, energy_is_approximate)"
        " VALUES (?, ?, 'fake text', ?, ?, ?)",
        (f"{local_date}T18:00:00Z", local_date, mood, energy, approx),
    )
    conn.commit()


def test_week_stats_counts_avg_and_series(conn):
    today = date(2026, 11, 1)  # a Sunday: the week is Mon 26 Oct to Sun 1 Nov
    add(conn, "2026-10-26", mood="good", energy=4)
    add(conn, "2026-10-26", mood="good", energy=8)
    add(conn, "2026-10-28", mood="low", energy=None)  # no energy: counted, not averaged
    add(conn, "2026-10-30", mood=None, energy=6, approx=1)  # imported, approximate: included
    add(conn, "2026-10-20", mood="sad", energy=2)  # last week: only in the 14-day series
    s = stats.week_stats(conn, today, TZ)
    assert s.entries == 4
    assert s.avg_energy == 6.0  # (4 + 8 + 6) / 3, the null is ignored
    assert s.mood_counts == {"good": 2, "low": 1}
    assert len(s.energy_series) == 14
    assert s.energy_series[0][0] == date(2026, 10, 19)
    assert s.energy_series[-1][0] == today
    by_day = dict(s.energy_series)
    assert by_day[date(2026, 10, 26)] == 6.0  # two entries that day: 4 and 8
    assert by_day[date(2026, 10, 28)] is None  # entry without energy
    assert by_day[date(2026, 10, 29)] is None  # no entry
    assert by_day[date(2026, 10, 30)] == 6.0
    assert by_day[date(2026, 10, 20)] == 2.0


def test_week_stats_empty(conn):
    s = stats.week_stats(conn, date(2026, 11, 1), TZ)
    assert s.entries == 0 and s.avg_energy is None and s.mood_counts == {}
    assert [v for _, v in s.energy_series] == [None] * 14


def test_week_stats_monday_to_sunday_boundary(conn):
    add(conn, "2026-10-25", energy=9)  # Sunday of the previous week
    add(conn, "2026-10-26", energy=3)  # Monday
    add(conn, "2026-11-01", energy=5)  # Sunday
    add(conn, "2026-11-02", energy=7)  # Monday of the next week
    sunday = stats.week_stats(conn, date(2026, 11, 1), TZ)
    assert sunday.entries == 2 and sunday.avg_energy == 4.0
    monday = stats.week_stats(conn, date(2026, 11, 2), TZ)
    assert monday.entries == 1 and monday.avg_energy == 7.0
    midweek = stats.week_stats(conn, date(2026, 10, 28), TZ)
    assert midweek.entries == 2  # Oct 26 and Nov 1 are both in that Monday-to-Sunday week
