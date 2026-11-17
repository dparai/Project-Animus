"""Change a renamed person's old name inside the entry text, only where the owner agrees.

After a rename the page lists the entries that use the old name for that person, with the words as
they are now and as they would be. Nothing changes until the owner ticks entries and confirms. Every
change keeps the old text (`entry_revisions`) so a whole batch can be undone. Only entries that
belong to this person count, so a nickname two people share ("dada") is never changed in an entry
the owner has not assigned to them."""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from . import people

SHOW = 70  # characters of context around the change
MAX_ENTRIES = 300

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(_FMT)


def _pattern(old: str, new: str) -> re.Pattern:
    # The new name first: text already written with it (e.g. "Munna dada" for "dada") is left alone.
    return re.compile(r"(?<!\w)(?:" + re.escape(new) + "|" + re.escape(old) + r")(?!\w)", re.IGNORECASE)


def _swap(text: str, old: str, new: str) -> tuple[str, int]:
    count = 0

    def one(m: re.Match) -> str:
        nonlocal count
        if m.group(0).casefold() == new.casefold():
            return m.group(0)
        count += 1
        return new

    return _pattern(old, new).sub(one, text), count


def _context(text: str, start: int, end: int) -> str:
    a, b = max(0, start - SHOW), min(len(text), end + SHOW)
    return ("…" if a else "") + text[a:b].replace("\n", " ") + ("…" if b < len(text) else "")


def candidates(conn: sqlite3.Connection, person_id: int, old: str) -> list[dict]:
    """Entries of this person that use `old`: what the words say now and what they would say."""
    person = people.get(conn, person_id)
    if person is None or old.casefold() == person["name"].casefold():
        return []
    new = person["name"]
    ids = people.mentions(conn).get(person_id, [])[-MAX_ENTRIES:]
    rows = people._entries(conn, ids)
    out = []
    for entry_id in reversed(ids):
        row = rows.get(entry_id)
        if row is None:
            continue
        after, count = _swap(row["text"], old, new)
        if not count:
            continue
        first = next(m for m in _pattern(old, new).finditer(row["text"]) if m.group(0).casefold() != new.casefold())
        shown = _context(row["text"], first.start(), first.end())
        out.append({"entry_id": entry_id, "date": row["local_date"], "count": count, "before": shown,
                    "after": _swap(shown, old, new)[0]})
    return out


def _put_text(conn, entry_id: int, text: str, source: str, target: str) -> None:
    """Set an entry's text. The facts noted for it are dropped by a trigger, so they are put back
    with the name swapped (a model pass would only find them again)."""
    row = conn.execute("SELECT data, model, created_at FROM entry_facts WHERE entry_id = ?", (entry_id,)).fetchone()
    conn.execute("UPDATE entries SET text = ? WHERE id = ?", (text, entry_id))
    if row is None:
        return
    try:
        facts = json.loads(row["data"])
        names = [target if n.casefold() == source.casefold() else n for n in facts.get("people", [])]
        facts["people"] = list(dict.fromkeys(names))
    except (ValueError, AttributeError):
        return
    conn.execute("INSERT OR REPLACE INTO entry_facts(entry_id, data, model, created_at) VALUES (?, ?, ?, ?)",
                 (entry_id, json.dumps(facts, ensure_ascii=False), row["model"], row["created_at"]))


def apply(conn: sqlite3.Connection, person_id: int, old: str, entry_ids: list[int]) -> tuple[str, int]:
    """Change the ticked entries. Returns (batch id for undo, entries changed)."""
    person = people.get(conn, person_id)
    if person is None:
        raise LookupError("person")
    allowed = {c["entry_id"] for c in candidates(conn, person_id, old)}
    batch, changed = uuid.uuid4().hex, 0
    with conn:
        for entry_id in dict.fromkeys(entry_ids):
            if entry_id not in allowed:
                continue
            text = conn.execute("SELECT text FROM entries WHERE id = ?", (entry_id,)).fetchone()["text"]
            after, count = _swap(text, old, person["name"])
            if not count:
                continue
            conn.execute(
                "INSERT INTO entry_revisions(batch, entry_id, old_text, new_text, person_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)", (batch, entry_id, text, after, person_id, _now()))
            _put_text(conn, entry_id, after, old, person["name"])
            changed += 1
    return batch, changed


def undo(conn: sqlite3.Connection, batch: str) -> int:
    """Put the old text back. An entry edited since is left alone. Returns entries restored."""
    rows = conn.execute("SELECT * FROM entry_revisions WHERE batch = ? AND undone = 0", (batch,)).fetchall()
    restored = 0
    with conn:
        for r in rows:
            now = conn.execute("SELECT text FROM entries WHERE id = ?", (r["entry_id"],)).fetchone()
            if now is None or now["text"] != r["new_text"]:
                continue
            person = people.get(conn, r["person_id"]) if r["person_id"] else None
            old_name = next((a for a in (person["aliases"] if person else [])
                             if a.casefold() in r["old_text"].casefold()), None)
            keep = person["name"] if person else ""
            _put_text(conn, r["entry_id"], r["old_text"], keep, old_name or keep)
            conn.execute("UPDATE entry_revisions SET undone = 1 WHERE id = ?", (r["id"],))
            restored += 1
    return restored


def recent_batches(conn: sqlite3.Connection, person_id: int) -> list[dict]:
    """Batches of changes for one person that can still be undone, newest first."""
    rows = conn.execute(
        "SELECT batch, COUNT(*) AS n, MAX(created_at) AS at FROM entry_revisions"
        " WHERE person_id = ? AND undone = 0 GROUP BY batch ORDER BY MAX(id) DESC LIMIT 5", (person_id,)).fetchall()
    return [{"batch": r["batch"], "count": r["n"], "at": r["at"][:10]} for r in rows]
