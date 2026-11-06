"""AI-suggested links between journal entries.

After a new entry is saved, older entries that look related are found with the existing FTS5
search, then one model call judges which of them are real follow-ups, recurrences, corrections
or plain relations. Results are stored as `suggested` links; the owner confirms or dismisses
them. Nothing is confirmed automatically.

Journal safety: logs hold only entry ids, counts and reason codes. Never entry text, prompts
or model output.
"""
import logging
import sqlite3
from datetime import datetime, timezone

from app import embeddings, search
from app.llm import LLMError

log = logging.getLogger("animus.links")

LINK_TYPES = ("follow_up", "recurrence", "correction", "related")
TYPE_LABELS = {
    "follow_up": "Follow-up to",
    "recurrence": "Happened before",
    "correction": "Corrects",
    "related": "Related to",
}
CANDIDATES = 8
SEMANTIC_CANDIDATES = 6
MAX_SUGGESTIONS = 3
CANDIDATE_CHARS = 700
NEW_ENTRY_CHARS = 2000
REASON_MAX = 140
LINK_TOKENS = 4096

SYSTEM = (
    "You help a private journal connect entries across time. You get one NEW entry and several "
    "OLDER entries. Pick only the older entries that clearly belong with the new one:\n"
    "- follow_up: the new entry is an update, outcome or consequence of the older one.\n"
    "- recurrence: the same feeling, trigger or situation happening again.\n"
    "- correction: the new entry fixes or changes the owner's own earlier view.\n"
    "- related: clearly the same topic, person or project, but none of the above.\n"
    "Be strict: sharing a common word is not a link. Choose at most "
    f"{MAX_SUGGESTIONS}; choosing none is normal and fine. Never invent ids. The reason is one "
    f"short plain sentence (under {REASON_MAX} characters) addressed to the owner as 'you'.\n"
    'Reply with JSON only: {"links": [{"id": <older entry id>, "type": "<type>", "reason": "<text>"}]}'
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_candidates(conn: sqlite3.Connection, entry_id: int, limit: int = CANDIDATES) -> list[sqlite3.Row]:
    """Older, not-yet-linked entries: closest in meaning first, then keyword matches.

    Meaning comes from stored embeddings (empty if none exist yet); keywords from FTS5."""
    row = conn.execute("SELECT id, text, created_at FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return []
    ids = [i for i, _ in embeddings.semantic_neighbours(conn, entry_id, SEMANTIC_CANDIDATES)]
    match = search.build_query(row["text"])
    if match is not None:
        for r in conn.execute(
            "SELECT e.id FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid"
            " WHERE entries_fts MATCH ? AND (e.created_at, e.id) < (?, ?)"
            " AND e.id NOT IN (SELECT to_entry_id FROM entry_links WHERE from_entry_id = ?)"
            " AND e.id NOT IN (SELECT from_entry_id FROM entry_links WHERE to_entry_id = ?)"
            " ORDER BY bm25(entries_fts), e.created_at DESC LIMIT ?",
            (match, row["created_at"], row["id"], entry_id, entry_id, limit),
        ):
            if r["id"] not in ids:
                ids.append(r["id"])
    ids = ids[: limit + SEMANTIC_CANDIDATES]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    by_id = {r["id"]: r for r in conn.execute(f"SELECT * FROM entries WHERE id IN ({marks})", ids)}
    return [by_id[i] for i in ids if i in by_id]


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build_prompt(new_row, candidates) -> str:
    parts = [f"NEW ENTRY (id {new_row['id']}, {new_row['local_date']}):\n{_clip(new_row['text'], NEW_ENTRY_CHARS)}\n",
             "OLDER ENTRIES:"]
    for c in candidates:
        parts.append(f"[id {c['id']}, {c['local_date']}]\n{_clip(c['text'], CANDIDATE_CHARS)}")
    return "\n\n".join(parts)


def parse_suggestions(result: dict, allowed_ids: set[int]) -> list[tuple[int, str, str]]:
    """Validated (older_id, type, reason) tuples; anything malformed is dropped."""
    out, seen = [], set()
    items = result.get("links") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        target, kind, reason = item.get("id"), item.get("type"), item.get("reason")
        if isinstance(target, bool) or not isinstance(target, int):
            continue
        if target not in allowed_ids or target in seen or kind not in LINK_TYPES:
            continue
        seen.add(target)
        out.append((target, kind, _clip(reason if isinstance(reason, str) else "", REASON_MAX)))
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def suggest_for_entry(conn: sqlite3.Connection, llm, entry_id: int, embedder=None) -> int:
    """Store AI link suggestions for one entry. Returns how many were stored; never raises.

    With an embedder, missing embeddings (this entry's first) are filled before searching; if
    that fails the keyword search still runs."""
    if embedder is not None:
        try:
            embeddings.fill_missing(conn, embedder)
        except LLMError as exc:
            log.info("embedding failed entry=%s reason=%s", entry_id, exc.reason)
        except Exception as exc:
            log.warning("embedding failed entry=%s error=%s", entry_id, type(exc).__name__)
    try:
        new_row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if new_row is None:
            return 0
        candidates = find_candidates(conn, entry_id)
        if not candidates:
            return 0
        result = llm.complete_json(SYSTEM, build_prompt(new_row, candidates),
                                   max_tokens=LINK_TOKENS, thinking=False)
        picks = parse_suggestions(result, {c["id"] for c in candidates})
        stored = 0
        for target, kind, reason in picks:
            cur = conn.execute(
                "INSERT OR IGNORE INTO entry_links(from_entry_id, to_entry_id, link_type, source,"
                " status, reason, created_at) VALUES (?, ?, ?, 'ai', 'suggested', ?, ?)",
                (entry_id, target, kind, reason, _now()),
            )
            stored += cur.rowcount
        conn.commit()
        log.info("link suggestions entry=%s candidates=%s stored=%s", entry_id, len(candidates), stored)
        return stored
    except LLMError as exc:
        log.info("link suggestions failed entry=%s reason=%s", entry_id, exc.reason)
    except Exception as exc:  # never let a background suggestion break anything
        log.warning("link suggestions failed entry=%s error=%s", entry_id, type(exc).__name__)
    return 0


def links_for_entry(conn: sqlite3.Connection, entry_id: int) -> dict:
    """Template-ready links around one entry: confirmed (both directions) and pending suggestions."""
    rows = conn.execute(
        "SELECT l.id, l.link_type, l.status, l.reason, l.from_entry_id, l.to_entry_id,"
        " o.id AS other_id, o.local_date AS other_date, o.text AS other_text,"
        " (l.from_entry_id = :eid) AS outgoing"
        " FROM entry_links l JOIN entries o ON o.id ="
        " CASE WHEN l.from_entry_id = :eid THEN l.to_entry_id ELSE l.from_entry_id END"
        " WHERE (l.from_entry_id = :eid OR l.to_entry_id = :eid) AND l.status IN ('confirmed','suggested')"
        " ORDER BY o.local_date, o.id",
        {"eid": entry_id},
    ).fetchall()
    confirmed, suggested = [], []
    for r in rows:
        item = {
            "id": r["id"], "other_id": r["other_id"], "date": r["other_date"],
            "snippet": _clip(r["other_text"], 90), "reason": r["reason"],
            "label": TYPE_LABELS[r["link_type"]] if r["outgoing"] else _incoming_label(r["link_type"]),
        }
        if r["status"] == "confirmed":
            confirmed.append(item)
        elif r["outgoing"]:  # suggestions are shown on the newer entry only
            suggested.append(item)
    return {"confirmed": confirmed, "suggested": suggested}


def _incoming_label(kind: str) -> str:
    return {
        "follow_up": "Followed up by",
        "recurrence": "Happened again",
        "correction": "Corrected by",
        "related": "Related to",
    }[kind]


def set_status(conn: sqlite3.Connection, entry_id: int, link_id: int, status: str) -> bool:
    """Confirm or dismiss a suggestion belonging to this entry (the newer end of the link)."""
    if status not in ("confirmed", "dismissed"):
        raise ValueError("status")
    cur = conn.execute(
        "UPDATE entry_links SET status = ? WHERE id = ? AND from_entry_id = ?",
        (status, link_id, entry_id),
    )
    conn.commit()
    return cur.rowcount == 1


def remove(conn: sqlite3.Connection, entry_id: int, link_id: int) -> bool:
    """Un-link: keep the row as dismissed so the same pair is not suggested again."""
    cur = conn.execute(
        "UPDATE entry_links SET status = 'dismissed' WHERE id = ?"
        " AND (from_entry_id = ? OR to_entry_id = ?)",
        (link_id, entry_id, entry_id),
    )
    conn.commit()
    return cur.rowcount == 1
