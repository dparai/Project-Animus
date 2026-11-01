import sqlite3
from datetime import datetime, timezone

_FMT = "%Y-%m-%dT%H:%M:%SZ"
STATUSES = ("active", "done", "failing")


def _ts(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(_FMT)


def add(conn: sqlite3.Connection, text: str, source_reflection_id: int | None, now: datetime,
        *, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO focus_areas(text, status, created_at, source_reflection_id)"
        " VALUES (?, 'active', ?, ?)",
        (text, _ts(now), source_reflection_id),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM focus_areas ORDER BY created_at DESC, id DESC").fetchall()


def set_status(conn: sqlite3.Connection, focus_id: int, status: str, now: datetime,
               *, commit: bool = True) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status!r}")
    completed_at = _ts(now) if status == "done" else None
    cur = conn.execute(
        "UPDATE focus_areas SET status = ?, completed_at = ? WHERE id = ?",
        (status, completed_at, focus_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def set_source(conn: sqlite3.Connection, focus_id: int, reflection_id: int, *, commit: bool = True) -> None:
    conn.execute("UPDATE focus_areas SET source_reflection_id = ? WHERE id = ?", (reflection_id, focus_id))
    if commit:
        conn.commit()
