import sqlite3
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Los_Angeles"
_KEY = "timezone"


def get_timezone(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (_KEY,)).fetchone()
    return row["value"] if row and row["value"] else DEFAULT_TZ


def set_timezone(conn: sqlite3.Connection, name: str) -> bool:
    try:
        ZoneInfo(name)
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError, TypeError
        raise ValueError(f"invalid timezone: {name!r}") from exc
    if name == get_timezone(conn):
        return False
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (_KEY, name),
    )
    conn.commit()
    return True
