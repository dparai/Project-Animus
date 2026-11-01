import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

SERIES_DAYS = 14


@dataclass(frozen=True)
class WeekStats:
    entries: int
    avg_energy: float | None
    mood_counts: dict[str, int]
    energy_series: list[tuple[date, float | None]]  # the last 14 local days, ending today


def week_stats(conn: sqlite3.Connection, today: date, tz: str) -> WeekStats:
    """Headline numbers for the Monday-to-Sunday week containing `today`.

    Days are the entries' own `local_date` (authoritative, never recomputed), so `tz` is not
    needed for grouping; it stays in the signature because callers always have it.
    Imported entries with approximate energy are included like any other.
    """
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    rows = conn.execute(
        "SELECT mood, energy_score FROM entries WHERE local_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    energies = [r["energy_score"] for r in rows if r["energy_score"] is not None]
    mood_counts: dict[str, int] = {}
    for r in rows:
        if r["mood"] is not None:
            mood_counts[r["mood"]] = mood_counts.get(r["mood"], 0) + 1

    first = today - timedelta(days=SERIES_DAYS - 1)
    per_day = {
        r["local_date"]: r["avg"]
        for r in conn.execute(
            "SELECT local_date, AVG(energy_score) AS avg FROM entries"
            " WHERE local_date BETWEEN ? AND ? AND energy_score IS NOT NULL GROUP BY local_date",
            (first.isoformat(), today.isoformat()),
        )
    }
    series = [
        (day, per_day.get(day.isoformat()))
        for day in (first + timedelta(days=i) for i in range(SERIES_DAYS))
    ]
    return WeekStats(
        entries=len(rows),
        avg_energy=sum(energies) / len(energies) if energies else None,
        mood_counts=mood_counts,
        energy_series=series,
    )
