import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app import moods


def local_date(utc_iso: str, tz: str) -> str:
    utc = datetime.strptime(utc_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return utc.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def _clean(text: str, mood: str | None, energy: int | None) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("text must not be blank")
    if mood is not None and not moods.is_valid(mood):
        raise ValueError(f"unknown mood: {mood!r}")
    if energy is not None and not (
        isinstance(energy, int) and not isinstance(energy, bool) and 1 <= energy <= 10
    ):
        raise ValueError("energy must be an integer from 1 to 10")
    return text


def create_entry(
    conn: sqlite3.Connection,
    *,
    text: str,
    mood: str | None,
    energy: int | None,
    now: datetime,
    tz: str,
    source_id: str | None = None,
    energy_is_approximate: bool = False,
) -> int:
    text = _clean(text, mood, energy)
    created_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute(
        "INSERT INTO entries(created_at, local_date, text, mood, energy_score,"
        " energy_is_approximate, source_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (created_at, local_date(created_at, tz), text, mood, energy,
         1 if energy_is_approximate else 0, source_id),
    )
    conn.commit()
    return cur.lastrowid


def update_entry(
    conn: sqlite3.Connection, entry_id: int, *, text: str, mood: str | None, energy: int | None
) -> None:
    text = _clean(text, mood, energy)
    # An edited energy is a real answer, so it is no longer an approximate import value.
    conn.execute(
        "UPDATE entries SET text = ?, mood = ?,"
        " energy_is_approximate = CASE WHEN energy_score IS ? THEN energy_is_approximate ELSE 0 END,"
        " energy_score = ? WHERE id = ?",
        (text, mood, energy, energy, entry_id),
    )
    conn.commit()


def get_entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    conn.commit()


def list_entries(
    conn: sqlite3.Connection, limit: int = 100, before: tuple[str, int] | None = None
) -> list[sqlite3.Row]:
    """Newest first. `before` is a (created_at, id) cursor: only rows strictly older are returned."""
    if before is None:
        return conn.execute(
            "SELECT * FROM entries ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM entries WHERE (created_at, id) < (?, ?)"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (before[0], before[1], limit),
    ).fetchall()
