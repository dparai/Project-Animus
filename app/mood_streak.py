"""Low-mood streak: a hint for the Counselor prompt only, never shown as a label or alert."""
import sqlite3
from datetime import date, timedelta

LOW_MOODS = ("low", "sad")


def low_mood_hint(
    conn: sqlite3.Connection,
    today: date,
    days: int = 7,
    threshold: int = 5,
    low_moods=LOW_MOODS,
) -> bool:
    """True when at least `threshold` distinct local days in the `days`-day window ending `today`
    have an entry with a low mood. Days are the entries' own `local_date` (never recomputed)."""
    low_moods = tuple(low_moods)
    if not low_moods or days < 1:
        return False
    start = today - timedelta(days=days - 1)
    marks = ",".join("?" * len(low_moods))
    n = conn.execute(
        f"SELECT COUNT(DISTINCT local_date) FROM entries"
        f" WHERE local_date BETWEEN ? AND ? AND mood IN ({marks})",
        (start.isoformat(), today.isoformat(), *low_moods),
    ).fetchone()[0]
    return n >= threshold
