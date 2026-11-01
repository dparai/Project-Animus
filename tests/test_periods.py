from datetime import date, datetime, timezone

from app import entries, periods

LA = "America/Los_Angeles"


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_week_is_monday_to_sunday():
    p = periods.week_of(date(2026, 10, 28))
    assert p == periods.Period("week", "2026-W44", date(2026, 10, 26), date(2026, 11, 1))
    assert p.start.weekday() == 0 and p.end.weekday() == 6


def test_sunday_night_counts_as_complete_after_21_local():
    week = periods.week_of(date(2026, 11, 1))
    before = utc(2026, 11, 2, 4, 59)  # Sun 20:59 PST (DST already ended)
    at = utc(2026, 11, 2, 5, 0)  # Sun 21:00 PST
    assert not periods.is_due("weekly", week, before, LA)
    assert periods.is_due("weekly", week, at, LA)
    assert periods.last_completed_week(before, LA).key == "2026-W43"
    assert periods.last_completed_week(at, LA).key == "2026-W44"


def test_monday_morning_week_complete():
    now = utc(2026, 11, 2, 16, 0)  # Mon 08:00 PST
    week = periods.last_completed_week(now, LA)
    assert (week.start, week.end) == (date(2026, 10, 26), date(2026, 11, 1))
    assert periods.is_due("weekly", week, now, LA)


def test_last_completed_month_before_and_after_6am_on_the_1st():
    before = utc(2026, 11, 1, 13, 59)  # Nov 1 05:59 PST
    at = utc(2026, 11, 1, 14, 0)  # Nov 1 06:00 PST
    assert periods.last_completed_month(before, LA).key == "2026-09"
    m = periods.last_completed_month(at, LA)
    assert (m.kind, m.key, m.start, m.end) == ("month", "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    assert periods.last_completed_month(utc(2026, 11, 20, 12, 0), LA).key == "2026-10"
    assert not periods.is_due("monthly", m, before, LA)
    assert periods.is_due("monthly", m, at, LA)


def test_year_boundary_week():
    assert date(2026, 12, 31).isocalendar()[:2] == (2026, 53)
    for d in (date(2026, 12, 31), date(2027, 1, 1)):
        p = periods.week_of(d)
        assert p.key == "2026-W53"
        assert (p.start, p.end) == (date(2026, 12, 28), date(2027, 1, 3))
        assert p.start.weekday() == 0 and p.end.weekday() == 6
    jan = periods.last_completed_month(utc(2027, 1, 5, 12, 0), LA)
    assert (jan.key, jan.start, jan.end) == ("2026-12", date(2026, 12, 1), date(2026, 12, 31))


def test_entries_for_uses_local_date_not_created_at(conn):
    inside = entries.create_entry(
        conn, text="late sunday", mood=None, energy=None, now=utc(2026, 11, 2, 3, 0), tz=LA
    )
    outside = entries.create_entry(
        conn, text="monday", mood=None, energy=None, now=utc(2026, 11, 2, 20, 0), tz=LA
    )
    first = entries.create_entry(
        conn, text="monday early", mood=None, energy=None, now=utc(2026, 10, 26, 15, 0), tz=LA
    )
    rows = periods.entries_for(conn, periods.week_of(date(2026, 11, 1)))
    assert [r["id"] for r in rows] == [first, inside]
    assert outside not in [r["id"] for r in rows]


def test_dst_change_week():
    p = periods.week_of(date(2026, 11, 1))
    assert (p.start, p.end) == (date(2026, 10, 26), date(2026, 11, 1))
    assert (p.end - p.start).days + 1 == 7
    assert p.start.weekday() == 0 and p.end.weekday() == 6
    assert not periods.is_due("weekly", p, utc(2026, 11, 2, 4, 59), LA)
    assert periods.is_due("weekly", p, utc(2026, 11, 2, 5, 0), LA)
