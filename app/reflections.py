import json
import sqlite3
from datetime import datetime, timezone

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def save(
    conn: sqlite3.Connection,
    *,
    kind: str,
    period_key: str,
    period_start: str,
    period_end: str,
    model: str,
    content: dict,
    now: datetime,
    commit: bool = True,
) -> int:
    prev = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM reflections WHERE kind = ? AND period_key = ?",
        (kind, period_key),
    ).fetchone()["v"]
    conn.execute(
        "UPDATE reflections SET is_latest = 0 WHERE kind = ? AND period_key = ?",
        (kind, period_key),
    )
    cur = conn.execute(
        "INSERT INTO reflections(kind, period_key, period_start, period_end, version,"
        " is_latest, model, content, created_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
        (kind, period_key, period_start, period_end, prev + 1, model,
         json.dumps(content, ensure_ascii=False), now.astimezone(timezone.utc).strftime(_FMT)),
    )
    if commit:
        conn.commit()
    return cur.lastrowid


def latest(conn: sqlite3.Connection, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reflections WHERE kind = ? ORDER BY period_end DESC, version DESC LIMIT 1",
        (kind,),
    ).fetchone()


def get(conn: sqlite3.Connection, reflection_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reflections WHERE id = ?", (reflection_id,)).fetchone()


def content(row: sqlite3.Row) -> dict:
    return json.loads(row["content"])
