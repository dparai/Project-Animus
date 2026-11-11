import sqlite3
from datetime import datetime, timezone

_FMT = "%Y-%m-%dT%H:%M:%SZ"
PROMPT_LIMIT = 40  # notes sent to the AI in each prompt
PROMPT_CORRECTIONS = 25  # of those, at most this many corrections; they always come first


def _ts(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime(_FMT)


def add_note(
    conn: sqlite3.Connection,
    *,
    kind: str,
    text: str,
    reflection_id: int | None = None,
    claim_id: str | None = None,
    conversation_id: int | None = None,
    now: datetime,
) -> int:
    cur = conn.execute(
        "INSERT INTO ai_memory(kind, text, reflection_id, claim_id, conversation_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (kind, text, reflection_id, claim_id, conversation_id, _ts(now)),
    )
    conn.commit()
    return cur.lastrowid


def list_active(conn: sqlite3.Connection, limit: int = PROMPT_LIMIT, qvec=None) -> list[sqlite3.Row]:
    """The notes sent to the AI: active corrections first (newest first, at most
    PROMPT_CORRECTIONS), then other notes (newest first) filling the rest up to `limit`.

    With a question vector and more notes than fit, the notes closest in meaning to the
    question are the ones kept (still shown newest first)."""
    corrections = conn.execute(
        "SELECT * FROM ai_memory WHERE deleted_at IS NULL AND kind = 'correction'"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (min(limit, PROMPT_CORRECTIONS),),
    ).fetchall()
    notes = conn.execute(
        "SELECT * FROM ai_memory WHERE deleted_at IS NULL AND kind != 'correction'"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (max(0, limit - len(corrections)),),
    ).fetchall()
    if qvec is not None and notes:
        from . import embeddings  # lazy: memory stays importable without the HTTP stack

        every = conn.execute(
            "SELECT id FROM ai_memory WHERE deleted_at IS NULL AND kind != 'correction'"
        ).fetchall()
        if len(every) > len(notes):
            keep = embeddings.rank_notes(conn, qvec, [r["id"] for r in every])[:len(notes)]
            marks = ",".join("?" * len(keep))
            notes = conn.execute(
                f"SELECT * FROM ai_memory WHERE id IN ({marks}) ORDER BY created_at DESC, id DESC", keep
            ).fetchall()
    return corrections + notes


def list_all_active(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Every active note of any kind, newest first (the Memory page)."""
    return conn.execute(
        "SELECT * FROM ai_memory WHERE deleted_at IS NULL"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def delete(conn: sqlite3.Connection, note_id: int, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE ai_memory SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (_ts(now), note_id),
    )
    conn.commit()
    return cur.rowcount > 0
