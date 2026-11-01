import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

WEEKLY_DUE = time(21, 0)  # Sunday, local
MONTHLY_DUE = time(6, 0)  # the 1st, local


@dataclass(frozen=True)
class Period:
    kind: str
    key: str
    start: date
    end: date  # inclusive


def week_of(day: date) -> Period:
    start = day - timedelta(days=day.weekday())
    year, week, _ = day.isocalendar()
    return Period("week", f"{year}-W{week:02d}", start, start + timedelta(days=6))


def _month_of(day: date) -> Period:
    start = day.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return Period("month", f"{start.year}-{start.month:02d}", start, nxt - timedelta(days=1))


def _due_at(kind: str, period: Period, tz: str) -> datetime:
    zone = ZoneInfo(tz)
    if kind == "weekly":
        return datetime.combine(period.end, WEEKLY_DUE, tzinfo=zone)
    if kind == "monthly":
        return datetime.combine(period.end + timedelta(days=1), MONTHLY_DUE, tzinfo=zone)
    raise ValueError(f"unknown kind: {kind!r}")


def due_instant(kind: str, period: Period, tz: str) -> datetime:
    return _due_at(kind, period, tz).astimezone(timezone.utc)


def is_due(kind: str, period: Period, now: datetime, tz: str) -> bool:
    return now >= due_instant(kind, period, tz)


def last_completed_week(now: datetime, tz: str) -> Period:
    current = week_of(now.astimezone(ZoneInfo(tz)).date())
    if is_due("weekly", current, now, tz):
        return current
    return week_of(current.start - timedelta(days=1))


def last_completed_month(now: datetime, tz: str) -> Period:
    this_month = _month_of(now.astimezone(ZoneInfo(tz)).date())
    previous = _month_of(this_month.start - timedelta(days=1))
    if is_due("monthly", previous, now, tz):
        return previous
    return _month_of(previous.start - timedelta(days=1))


def entries_for(conn: sqlite3.Connection, period: Period) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE local_date BETWEEN ? AND ? ORDER BY created_at, id",
        (period.start.isoformat(), period.end.isoformat()),
    ).fetchall()
