"""Writing calendar for the Entries page: one square per day, a year of weeks, plus streaks.

Everything here is counted in code from `entries.local_date` (the day an entry was written, in the
owner's time zone at that moment). No model call, no entry text.
"""
import sqlite3
from datetime import date, timedelta

from app.moods import MOODS

WEEKS = 53
MAX_LEVEL = 4
_MOOD_LABEL = {m.key: m.label for m in MOODS}


def _streaks(days: list[date], today: date) -> tuple[int, int]:
    """(current, longest) run of consecutive days. Today not written yet does not break the run."""
    longest = run = 0
    previous = None
    for day in days:
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day
    written = set(days)
    cursor = today if today in written else today - timedelta(days=1)
    current = 0
    while cursor in written:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


def _label(day: date, count: int, mood: str | None) -> str:
    text = f"{day:%a} {day.day} {day:%b} {day.year}: "
    text += "no entry" if not count else f"{count} {'entry' if count == 1 else 'entries'}"
    if mood:
        text += f", {_MOOD_LABEL[mood].lower()}"
    return text


def build(conn: sqlite3.Connection, today: date, weeks: int = WEEKS) -> dict:
    """Template-ready calendar ending in the week that holds `today`. Weeks start on Sunday."""
    counts: dict[str, int] = {}
    moods: dict[str, str] = {}
    for row in conn.execute("SELECT local_date, mood FROM entries ORDER BY created_at, id"):
        key = row["local_date"]
        counts[key] = counts.get(key, 0) + 1
        if row["mood"]:
            moods[key] = row["mood"]  # the day's latest mood wins

    this_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    start = this_sunday - timedelta(weeks=weeks - 1)
    columns = []
    last_month = None
    for index in range(weeks):
        sunday = start + timedelta(weeks=index)
        cells = []
        month = ""
        for offset in range(7):
            day = sunday + timedelta(days=offset)
            key = day.isoformat()
            count = counts.get(key, 0)
            if day <= today and day.month != last_month and day.day <= 7:
                month = f"{day:%b}"
                last_month = day.month
            cells.append({
                "date": key,
                "future": day > today,
                "count": count,
                "level": min(count, MAX_LEVEL),
                "mood": moods.get(key),
                "label": _label(day, count, moods.get(key)),
            })
        columns.append({"index": index, "month": month, "cells": cells})

    days = sorted(date.fromisoformat(key) for key in counts)
    current, longest = _streaks(days, today)
    return {
        "columns": columns,
        "days_written": len(days),
        "entries": sum(counts.values()),
        "streak": current,
        "longest": longest,
        "moods": [{"key": m.key, "label": m.label} for m in MOODS],
    }
