"""Counselor chat: storage, retrieval (prepare, DB only) and reply (LLM only, no DB)."""
import calendar
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

from . import memory, mood_streak, prompts, search
from .llm import LLMError, interactive_models
from .memory import _ts
from .reflect import _int, _list, _model_name, _str

log = logging.getLogger("animus.counselor")

MESSAGE_MAX = 2000
REPLY_MAX = 1200
HISTORY_LIMIT = 20
TOP_ENTRIES = 4
QUERY_MESSAGES = 3  # the owner's last messages joined into the retrieval query
READ_TIMEOUT = 90.0
MAX_CALLS = 2
REPLY_TOKENS = 4096  # room for reasoning models to think before the reply
TIME_BUDGET = 45.0  # no retry once the first call has taken longer than this
LIST_LIMIT = 30

NEUTRAL_REPLY = "I'm here. Tell me more about what's on your mind."


def _clock() -> float:
    return time.monotonic()


_ERROR_TEXTS = {
    "no_key": "No API key is configured, so I cannot reply yet. Your message is saved.",
    "timeout": "The model took too long to reply. Your message is saved; please try again in a moment.",
    "unreachable": "I could not reach the model just now. Your message is saved; please try again in a moment.",
    "http_429": "The model is busy right now (rate limit). Your message is saved; please try again in a minute.",
    "empty": "I could not put a reply together this time. Your message is saved; please try again.",
    "error": "The model could not reply this time. Your message is saved; please try again later.",
}


def error_text(reason: str) -> str:
    return _ERROR_TEXTS.get(reason, _ERROR_TEXTS["error"])


@dataclass(frozen=True)
class CounselorContext:
    history: tuple   # oldest first: {"role", "text"}
    entries: tuple   # {"id", "local_date", "text", "mood", "energy_score"}
    notes: tuple     # {"kind", "text", "created_at"}
    low_mood: bool = False
    answering_id: int | None = None  # id of the owner's message this reply answers


@dataclass(frozen=True)
class CounselorResult:
    text: str
    cited_entry_ids: tuple = ()
    error: str | None = None
    model: str | None = None
    answering_id: int | None = None


# --- storage -----------------------------------------------------------------

def _conversation(conn, conv_id):
    return conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()


def get_conversation(conn, conv_id):
    """The conversation row, or None for an unknown, non-integer or out-of-range id."""
    if isinstance(conv_id, bool) or not isinstance(conv_id, int) or not 0 < conv_id < 2**63:
        return None
    return _conversation(conn, conv_id)


def _last_message_id(conn, conv_id):
    return conn.execute(
        "SELECT MAX(id) FROM messages WHERE conversation_id = ?", (conv_id,)
    ).fetchone()[0]


def start(conn, now: datetime) -> int:
    ts = _ts(now)
    cur = conn.execute(
        "INSERT INTO conversations(started_at, last_message_at) VALUES (?, ?)", (ts, ts)
    )
    conn.commit()
    return cur.lastrowid


def _insert(conn, conv_id, role: str, text: str, cited, now: datetime) -> int:
    ts = _ts(now)
    cur = conn.execute(
        "INSERT INTO messages(conversation_id, role, text, cited_entry_ids, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (conv_id, role, text, json.dumps(list(cited)), ts),
    )
    conn.execute("UPDATE conversations SET last_message_at = ? WHERE id = ?", (ts, conv_id))
    if role == "user":
        # A resumed chat is summarized again later; its new notes replace the earlier ones.
        conn.execute(
            "UPDATE conversations SET summarized_at = NULL, summary_attempts = 0, last_attempt_at = NULL"
            " WHERE id = ? AND ended_at IS NULL AND summarized_at IS NOT NULL",
            (conv_id,),
        )
    conn.commit()
    return cur.lastrowid


def add_user_message(conn, conv_id: int, text, now: datetime) -> int:
    """Saves (and commits) the owner's message. Call this before any model call."""
    text = text.strip() if isinstance(text, str) else ""
    if not text or len(text) > MESSAGE_MAX:
        raise ValueError("text")
    conv = _conversation(conn, conv_id)
    if conv is None:
        raise ValueError("conversation")
    if conv["ended_at"] is not None:
        raise ValueError("ended")
    return _insert(conn, conv_id, "user", text, (), now)


def save_reply(conn, conv_id: int, result: CounselorResult, now: datetime) -> int | None:
    """Stores a successful reply. Saves nothing on error, when the conversation is gone or
    ended, or when a newer message arrived after the one this reply answers."""
    if result.error or not result.text or result.answering_id is None:
        return None
    conv = _conversation(conn, conv_id)
    if conv is None or conv["ended_at"] is not None:
        return None
    if _last_message_id(conn, conv_id) != result.answering_id:
        return None
    return _insert(conn, conv_id, "assistant", result.text, result.cited_entry_ids, now)


def end(conn, conv_id: int, now: datetime) -> bool:
    cur = conn.execute(
        "UPDATE conversations SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (_ts(now), conv_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete(conn, conv_id: int, now: datetime | None = None) -> bool:
    """Deletes the chat (messages cascade) and, in the same transaction, soft deletes the memory
    notes made from it."""
    now = now or datetime.now(timezone.utc)
    with conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        if cur.rowcount:
            conn.execute(
                "UPDATE ai_memory SET deleted_at = ? WHERE conversation_id = ? AND deleted_at IS NULL",
                (_ts(now), conv_id),
            )
    return cur.rowcount > 0


def list_conversations(conn, limit: int = LIST_LIMIT) -> list:
    return conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count"
        " FROM conversations c ORDER BY c.last_message_at DESC, c.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _cited(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [i for i in _list(value) if isinstance(i, int) and not isinstance(i, bool)]


def messages(conn, conv_id: int) -> list[dict]:
    """All messages, oldest first; cited_entry_ids parsed (resolve them at render time)."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()
    return [
        {"id": r["id"], "role": r["role"], "text": r["text"],
         "cited_entry_ids": _cited(r["cited_entry_ids"]), "created_at": r["created_at"]}
        for r in rows
    ]


# --- prepare (DB reads only) ---------------------------------------------------

def query_text(conn, conv_id: int) -> str:
    """The owner's latest messages joined: what the journal is searched for."""
    owner = conn.execute(
        "SELECT text FROM messages WHERE conversation_id = ? AND role = 'user'"
        " ORDER BY id DESC LIMIT ?",
        (conv_id, QUERY_MESSAGES),
    ).fetchall()
    return " ".join(r["text"] for r in reversed(owner))


def prepare(conn, conv_id: int, today: date, qvec=None) -> CounselorContext:
    if _conversation(conn, conv_id) is None:
        raise ValueError("conversation")
    rows = conn.execute(
        "SELECT role, text FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conv_id, HISTORY_LIMIT),
    ).fetchall()
    history = tuple({"role": r["role"], "text": r["text"]} for r in reversed(rows))
    owner = conn.execute(
        "SELECT id, text FROM messages WHERE conversation_id = ? AND role = 'user'"
        " ORDER BY id DESC LIMIT ?",
        (conv_id, QUERY_MESSAGES),
    ).fetchall()
    answering_id = owner[0]["id"] if owner else None
    query = " ".join(r["text"] for r in reversed(owner))
    found = search.find_entries_hybrid(conn, query, today, TOP_ENTRIES, qvec) if query else []
    entries = tuple(
        {"id": r["id"], "local_date": r["local_date"], "text": r["text"],
         "mood": r["mood"], "energy_score": r["energy_score"]}
        for r in found[:TOP_ENTRIES]
    )
    notes = tuple(
        {"kind": n["kind"], "text": n["text"], "created_at": n["created_at"],
         "conversation_id": n["conversation_id"]}
        for n in memory.list_active(conn, qvec=qvec)
    )
    return CounselorContext(history=history, entries=entries, notes=notes,
                            low_mood=mood_streak.low_mood_hint(conn, today),
                            answering_id=answering_id)


# --- date guard -----------------------------------------------------------------
# Only date-like claims are checked: month names, 4-digit years, numeric and ISO dates,
# day+month pairs and counted spans ("3 weeks", "4 times"). Ordinary numbers pass.

_MONTH_NUM = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTH_NUM.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTH_NUM["sept"] = 9
_MONTH_ALT = "|".join(sorted(_MONTH_NUM, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th)?"
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_NUM_DATE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2}|\d{4}))?\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_ALT})\b", re.IGNORECASE)
_MONTH_DAY = re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}}){_ORD}\b(?!\s*(?::|\d))", re.IGNORECASE)
_MONTH = re.compile(rf"\b({_MONTH_ALT})\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SPAN = re.compile(r"\b(\d+)\s+(day|week|month|year)s?\b", re.IGNORECASE)
_TIMES = re.compile(r"\b(\d+)\s+times\b", re.IGNORECASE)


def _month(word: str):
    if word == "may" or word == "march":  # ordinary lower-case words
        return None
    return _MONTH_NUM.get(word.lower())


def _day_tokens(month: int, day: int) -> set:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return set()
    return {("md", month, day), ("dm", tuple(sorted((month, day))))}


def _date_tokens(text: str) -> set:
    out = set()
    rest = text
    for m in _ISO_DATE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        out |= {("y", y)} | _day_tokens(mo, d)
        if 1 <= mo <= 12:
            out.add(("m", mo))
    rest = _ISO_DATE.sub(" ", rest)
    for m in _NUM_DATE.finditer(rest):
        a, b = int(m.group(1)), int(m.group(2))
        out.add(("dm", tuple(sorted((a, b)))))
        if m.group(3):
            y = int(m.group(3))
            out.add(("y", y + 2000 if y < 100 else y))
    rest = _NUM_DATE.sub(" ", rest)
    for m in _DAY_MONTH.finditer(rest):
        mo = _month(m.group(2))
        if mo:
            out |= _day_tokens(mo, int(m.group(1)))
    for m in _MONTH_DAY.finditer(rest):
        mo = _month(m.group(1))
        if mo:
            out |= _day_tokens(mo, int(m.group(2)))
    for m in _MONTH.finditer(rest):
        mo = _month(m.group(1))
        if mo:
            out.add(("m", mo))
    out |= {("y", int(y)) for y in _YEAR.findall(rest)}
    out |= {("span", int(n), u.lower()) for n, u in _SPAN.findall(rest)}
    out |= {("times", int(n)) for n in _TIMES.findall(rest)}
    return out


def _support(ctx: CounselorContext) -> set:
    """Date claims the reply may make: retrieved entries (text and local_date), the chat
    history and the owner's memory notes."""
    have = set()
    for e in ctx.entries:
        have |= _date_tokens(e["text"])
        have |= _date_tokens(str(e["local_date"]))
    for m in ctx.history:
        have |= _date_tokens(m["text"])
    for n in ctx.notes:
        have |= _date_tokens(n["text"])
    return have


# --- reply (LLM only, no DB) ---------------------------------------------------

_END = re.compile(r"[.!?…]+[\"'”’)\]]*(?=\s|$)")  # a sentence end followed by space or the end
_PARA = re.compile(r"\n\s*\n")
_INLINE_WS = re.compile(r"[ \t\r\f\v]+")


def _clean(text: str) -> str:
    """Unescape what the model copied from the prompt, tidy whitespace, keep paragraph breaks."""
    text = prompts.unescape(text).replace("\r\n", "\n")
    paras = [_INLINE_WS.sub(" ", p.replace("\n", " ")).strip() for p in _PARA.split(text)]
    return "\n\n".join(p for p in paras if p)


def _sentences(paragraph: str) -> list[str]:
    out, pos = [], 0
    for m in _END.finditer(paragraph):
        out.append(paragraph[pos:m.end()].strip())
        pos = m.end()
    out.append(paragraph[pos:].strip())
    return [s for s in out if s]


def _drop_unbacked(text: str, have: set) -> str:
    """Remove sentences whose date claims are not backed by the retrieved data."""
    kept_paras = []
    for para in text.split("\n\n"):
        kept = [s for s in _sentences(para) if _date_tokens(s) <= have]
        if kept:
            kept_paras.append(" ".join(kept))
    return "\n\n".join(kept_paras)


def _cap(text: str) -> str:
    if len(text) <= REPLY_MAX:
        return text
    head = text[:REPLY_MAX]
    best = -1
    for m in _END.finditer(head):
        best = m.end()
    if best > 0:
        return head[:best].rstrip()
    cut = head[: REPLY_MAX - 1].rsplit(None, 1)[0] if " " in head else head[: REPLY_MAX - 1]
    return cut.rstrip() + "…"


def _verify(raw, ctx: CounselorContext) -> tuple[str, tuple]:
    """Returns (text, cited ids). Text is "" when the model gave nothing, NEUTRAL_REPLY (with no
    citations) when every sentence made an unbacked date claim."""
    raw = raw if isinstance(raw, dict) else {}
    cleaned = _clean(_str(raw.get("reply")))
    if not cleaned:
        return "", ()
    text = _cap(_drop_unbacked(cleaned, _support(ctx)))
    if not text:
        return NEUTRAL_REPLY, ()
    allowed = {e["id"] for e in ctx.entries}
    ids: list[int] = []
    for item in _list(raw.get("cited_entry_ids")):
        eid = _int(item)
        if eid in allowed and eid not in ids:
            ids.append(eid)
    return text, tuple(ids)


def reply(llm, ctx: CounselorContext) -> CounselorResult:
    """Pure: no DB. Returns a fixed friendly text with a reason code on any failure; never raises."""
    aid = ctx.answering_id

    def failed(reason):
        return CounselorResult(text=error_text(reason), error=reason, answering_id=aid)

    if not any(m["role"] == "user" for m in ctx.history):
        return failed("empty")
    try:
        system = prompts.counselor_system()
        user = prompts.counselor_user(ctx.history, ctx.entries, ctx.notes, ctx.low_mood)
    except Exception as exc:  # malformed context; never raise to the page
        log.warning("counselor prompt failed error=%s", type(exc).__name__)
        return failed("error")
    started = _clock()
    for attempt in range(MAX_CALLS):
        if attempt and _clock() - started > TIME_BUDGET:
            log.info("counselor retry skipped, time budget used")
            break
        prompt = user if attempt == 0 else user + "\n\n" + prompts.counselor_retry_note()
        try:
            raw = llm.complete_json(system, prompt, max_tokens=REPLY_TOKENS, read_timeout=READ_TIMEOUT,
                                  models=interactive_models(), thinking=False)
        except LLMError as exc:
            log.warning("counselor failed reason=%s", exc.reason)
            return failed(exc.reason)
        except Exception as exc:
            log.warning("counselor failed error=%s", type(exc).__name__)
            return failed("error")
        try:
            text, ids = _verify(raw, ctx)
        except Exception as exc:
            log.warning("counselor verify failed error=%s", type(exc).__name__)
            text, ids = "", ()
        if text:
            return CounselorResult(text=text, cited_entry_ids=ids, model=_model_name(llm),
                                   answering_id=aid)
        log.info("counselor empty reply attempt=%d", attempt + 1)
    return failed("empty")
