"""Memory from counselor conversations: short notes about the owner, fed into AI memory.

A finished (ended, or idle for IDLE_HOURS) conversation with at least MIN_USER_MESSAGES owner
messages is summarized into at most MAX_NOTES one-line notes, stored as `ai_memory` rows of kind
'note' linked by `conversation_id`. A resumed conversation is summarized again later and its new
notes replace the earlier ones. When active notes pass NOTE_CAP, the oldest MERGE_BATCH are merged
into at most MERGE_MAX notes (originals soft deleted). Correction notes are never touched.

Journal safety: logs hold only conversation ids, counts, statuses and reason codes. Never chat
text, notes, prompts or model output.
"""
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import prompts
from .db import connect
from .llm import LLMError, interactive_models
from .memory import _ts

log = logging.getLogger("animus.chat_memory")

MIN_USER_MESSAGES = 2
IDLE_HOURS = 6
MAX_NOTES = 3
NOTE_MAX_CHARS = 240
PER_TICK = 3  # conversations stored per tick
MAX_CALLS_PER_TICK = 3  # model calls per tick in total (summaries, failures and the merge)
MAX_ATTEMPTS = 3  # failed summaries before a conversation is given up (marked, no notes)
RETRY_COOLDOWN = timedelta(hours=1)
READ_TIMEOUT = 120.0  # shorter than reflection jobs, so one tick stays bounded
# Consolidation starts before the prompt window (memory.PROMPT_LIMIT) has to drop notes.
NOTE_CAP = 30
MERGE_BATCH = 15
MERGE_MAX = 5
MERGE_COOLDOWN = timedelta(hours=1)
MERGE_FAILED_KEY = "memory_merge_failed_at"
SUMMARY_MESSAGES = 40  # the newest messages of a conversation sent for summarizing
MESSAGE_CHARS = 2000
SUMMARY_TOKENS = 4096
MERGE_TOKENS = 4096

_WS = re.compile(r"\s+")
_step_lock = threading.Lock()


@dataclass(frozen=True)
class SummaryResult:
    notes: tuple = ()
    ok: bool = True
    reason: str = ""
    replaces: tuple = ()  # ids of older notes these notes make outdated


def clean_notes(raw, limit: int) -> tuple | None:
    """Validate a model's `notes` list. None when the shape is wrong; otherwise one-line,
    length-capped, deduplicated strings (non-strings and empty ones dropped), at most `limit`."""
    if not isinstance(raw, list):
        return None
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, str):
            continue
        text = _WS.sub(" ", item).strip()
        if len(text) > NOTE_MAX_CHARS:
            text = text[:NOTE_MAX_CHARS].rstrip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) == limit:
            break
    return tuple(out)


# --- candidates ------------------------------------------------------------------------------

_CANDIDATES = """
SELECT c.id, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id AND m.role = 'user') AS users
FROM conversations c
WHERE c.summarized_at IS NULL AND (c.ended_at IS NOT NULL OR c.last_message_at < ?)
  AND (c.last_attempt_at IS NULL OR c.last_attempt_at < ?)
ORDER BY c.last_message_at, c.id
"""


def due_conversations(conn: sqlite3.Connection, now: datetime) -> list[int]:
    """Finished conversations (ended, or idle over IDLE_HOURS) not yet summarized and not tried
    within RETRY_COOLDOWN, oldest first. Finished ones with fewer than MIN_USER_MESSAGES owner
    messages are marked summarized here (no notes, no model call) so they stop being candidates."""
    rows = conn.execute(
        _CANDIDATES, (_ts(now - timedelta(hours=IDLE_HOURS)), _ts(now - RETRY_COOLDOWN))
    ).fetchall()
    short = [r["id"] for r in rows if r["users"] < MIN_USER_MESSAGES]
    if short:
        with conn:
            conn.executemany(
                "UPDATE conversations SET summarized_at = ? WHERE id = ? AND summarized_at IS NULL",
                [(_ts(now), cid) for cid in short],
            )
    return [r["id"] for r in rows if r["users"] >= MIN_USER_MESSAGES]


def conversation_messages(conn: sqlite3.Connection, conv_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, text FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conv_id, SUMMARY_MESSAGES),
    ).fetchall()
    return [{"role": r["role"], "text": r["text"]} for r in reversed(rows)]


# --- summarize (pure) ------------------------------------------------------------------------

def active_notes(conn: sqlite3.Connection) -> list[dict]:
    """The automatic notes now in memory (newest first), offered so new notes can replace them."""
    rows = conn.execute(
        "SELECT id, text FROM ai_memory WHERE kind = 'note' AND deleted_at IS NULL"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (NOTE_CAP + MERGE_BATCH,),
    ).fetchall()
    return [{"id": r["id"], "text": r["text"]} for r in rows]


def _replaces(raw, existing) -> tuple:
    allowed = {n["id"] for n in existing or ()}
    out = []
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, int) and not isinstance(item, bool) and item in allowed and item not in out:
            out.append(item)
    return tuple(out)


def summarize(llm, messages, existing=None) -> SummaryResult:
    """Ask the model for up to MAX_NOTES notes about the owner. No database access.
    An empty list is a valid result; errors come back as ok=False with a reason code."""
    history = [{"role": m["role"], "text": str(m["text"])[:MESSAGE_CHARS]} for m in messages]
    try:
        raw = llm.complete_json(prompts.chat_summary_system(), prompts.chat_summary_user(history, existing),
                                max_tokens=SUMMARY_TOKENS, read_timeout=READ_TIMEOUT,
                                models=interactive_models())
    except LLMError as exc:
        return SummaryResult(ok=False, reason=exc.reason or type(exc).__name__)
    except Exception as exc:  # class name only, never the message
        return SummaryResult(ok=False, reason=type(exc).__name__)
    notes = clean_notes(raw.get("notes") if isinstance(raw, dict) else None, MAX_NOTES)
    if notes is None:
        return SummaryResult(ok=False, reason="invalid_output")
    replaces = _replaces(raw.get("replaces"), existing) if notes else ()
    return SummaryResult(notes=notes, replaces=replaces)


# --- store -----------------------------------------------------------------------------------

def _cited_entries(conn, conv_id: int) -> str:
    """JSON list of the entry ids the listener drew on in this chat (the notes' sources)."""
    ids: list[int] = []
    for r in conn.execute(
        "SELECT cited_entry_ids FROM messages WHERE conversation_id = ? AND role = 'assistant'", (conv_id,)
    ):
        try:
            for i in json.loads(r["cited_entry_ids"] or "[]"):
                if isinstance(i, int) and not isinstance(i, bool) and i not in ids:
                    ids.append(i)
        except ValueError:
            continue
    return json.dumps(ids[:20])


def store(conn: sqlite3.Connection, conv_id: int, notes, now: datetime,
          last_message_at: str | None = None, replaces=()) -> int:
    """One transaction: mark the conversation summarized, soft delete the notes an earlier
    summary of it produced, and add the new ones; or nothing at all. With `last_message_at`
    (read before the model call) nothing is stored when a newer message arrived meanwhile, so the
    next tick summarizes the whole chat. Returns the number of notes added."""
    ts = _ts(now)
    with conn:
        cur = conn.execute(
            "UPDATE conversations SET summarized_at = ? WHERE id = ? AND summarized_at IS NULL"
            " AND (? IS NULL OR last_message_at = ?)",
            (ts, conv_id, last_message_at, last_message_at),
        )
        if cur.rowcount != 1:
            return 0
        conn.execute(
            "UPDATE ai_memory SET deleted_at = ? WHERE conversation_id = ? AND kind = 'note'"
            " AND deleted_at IS NULL",
            (ts, conv_id),
        )
        sources = _cited_entries(conn, conv_id)
        first = None
        for text in notes:
            cur = conn.execute(
                "INSERT INTO ai_memory(kind, text, conversation_id, created_at, source_entry_ids)"
                " VALUES ('note', ?, ?, ?, ?)",
                (text, conv_id, ts, sources),
            )
            first = first or cur.lastrowid
        if first is not None:
            # An older note the new ones contradict or update is retired, with a pointer to why.
            conn.executemany(
                "UPDATE ai_memory SET deleted_at = ?, superseded_by = ? WHERE id = ? AND kind = 'note'"
                " AND deleted_at IS NULL",
                [(ts, first, nid) for nid in replaces],
            )
    return len(notes)


def record_failure(conn: sqlite3.Connection, conv_id: int, now: datetime) -> bool:
    """Count a failed summary. True when MAX_ATTEMPTS is reached: the conversation is then
    marked summarized with no notes so it stops being retried."""
    ts = _ts(now)
    with conn:
        conn.execute(
            "UPDATE conversations SET summary_attempts = summary_attempts + 1, last_attempt_at = ?,"
            " summarized_at = CASE WHEN summary_attempts + 1 >= ? THEN ? ELSE NULL END"
            " WHERE id = ? AND summarized_at IS NULL",
            (ts, MAX_ATTEMPTS, ts, conv_id),
        )
    row = conn.execute("SELECT summarized_at FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    return row is not None and row[0] is not None


# --- consolidation ---------------------------------------------------------------------------

def _active_note_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM ai_memory WHERE kind = 'note' AND deleted_at IS NULL"
    ).fetchone()[0]


def _merge_failed(conn, now: datetime, reason: str) -> bool:
    log.warning("memory merge failed reason=%s", reason)
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (MERGE_FAILED_KEY, _ts(now)),
        )
    return False


def consolidate_if_needed(conn: sqlite3.Connection, llm, now: datetime) -> bool:
    """When active 'note' notes exceed NOTE_CAP, merge the oldest MERGE_BATCH into at most
    MERGE_MAX. True when a merge was stored. Any model error or invalid output changes no note,
    and no new attempt is made for MERGE_COOLDOWN after a failure."""
    if _active_note_count(conn) <= NOTE_CAP:
        return False
    failed = conn.execute("SELECT value FROM settings WHERE key = ?", (MERGE_FAILED_KEY,)).fetchone()
    if failed and failed[0] and failed[0] > _ts(now - MERGE_COOLDOWN):
        return False
    oldest = conn.execute(
        "SELECT id, text, created_at FROM ai_memory WHERE kind = 'note' AND deleted_at IS NULL"
        " ORDER BY created_at, id LIMIT ?",
        (MERGE_BATCH,),
    ).fetchall()
    try:
        raw = llm.complete_json(prompts.memory_merge_system(), prompts.memory_merge_user(oldest),
                                max_tokens=MERGE_TOKENS, read_timeout=READ_TIMEOUT,
                                models=interactive_models())
    except LLMError as exc:
        return _merge_failed(conn, now, exc.reason or type(exc).__name__)
    except Exception as exc:
        return _merge_failed(conn, now, type(exc).__name__)
    candidate = raw.get("notes") if isinstance(raw, dict) else None
    # More than MERGE_MAX items means the model ignored the rules: nothing is stored.
    merged = clean_notes(candidate, MERGE_MAX + 1)
    if not merged or len(merged) > MERGE_MAX:
        return _merge_failed(conn, now, "invalid_output")
    ids = [r["id"] for r in oldest]
    # Merged notes keep the age of their newest source, so later notes still sort newer.
    stamp = oldest[-1]["created_at"]
    with conn:
        cur = conn.executemany(
            "UPDATE ai_memory SET deleted_at = ? WHERE id = ? AND kind = 'note' AND deleted_at IS NULL",
            [(_ts(now), nid) for nid in ids],
        )
        if cur.rowcount != len(ids):  # the owner deleted one meanwhile: try again next tick
            conn.rollback()
            log.info("memory merge skipped reason=changed")
            return False
        conn.executemany(
            "INSERT INTO ai_memory(kind, text, created_at) VALUES ('note', ?, ?)",
            [(text, stamp) for text in merged],
        )
        conn.execute("DELETE FROM settings WHERE key = ?", (MERGE_FAILED_KEY,))
    log.info("memory merge done merged=%d into=%d", len(ids), len(merged))
    return True


# --- scheduler step --------------------------------------------------------------------------

def run_step(db_path: str, llm, now: datetime) -> list[int]:
    """One scheduler pass: summarize due conversations one at a time (up to PER_TICK stored and
    MAX_CALLS_PER_TICK model calls), then the consolidation check. Runs in the scheduler's worker
    thread, never on the event loop. A failed conversation is passed over and retried after
    RETRY_COOLDOWN, up to MAX_ATTEMPTS. Returns the ids stored."""
    if not _step_lock.acquire(blocking=False):
        return []
    try:
        conn = connect(db_path)
        try:
            return _run(conn, llm, now)
        finally:
            conn.close()
    finally:
        _step_lock.release()


def _run(conn, llm, now: datetime) -> list[int]:
    from .jobs import resolve_llm  # lazy: the real client is built only when there is work

    done = []
    client = None
    calls = 0
    for conv_id in due_conversations(conn, now):
        if len(done) >= PER_TICK or calls >= MAX_CALLS_PER_TICK:
            break
        row = conn.execute("SELECT last_message_at FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            continue
        client = client or resolve_llm(llm)
        calls += 1
        result = summarize(client, conversation_messages(conn, conv_id), active_notes(conn))
        if not result.ok:
            if result.reason == "no_key":
                log.warning("conversation summary id=%s status=failed reason=no_key", conv_id)
                return done  # nothing else can succeed this tick; no attempt counted
            status = "gave_up" if record_failure(conn, conv_id, now) else "failed"
            log.warning("conversation summary id=%s status=%s reason=%s", conv_id, status, result.reason)
            continue
        added = store(conn, conv_id, result.notes, now, last_message_at=row["last_message_at"],
                      replaces=result.replaces)
        stored = conn.execute(
            "SELECT summarized_at FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        if stored is None or stored[0] is None:
            log.info("conversation summary id=%s status=skipped reason=changed", conv_id)
            continue
        log.info("conversation summary id=%s status=done notes=%d", conv_id, added)
        done.append(conv_id)
    if calls < MAX_CALLS_PER_TICK and _active_note_count(conn) > NOTE_CAP:
        consolidate_if_needed(conn, client or resolve_llm(llm), now)
    return done
