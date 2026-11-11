import calendar
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from . import embeddings

STOP_WORDS = frozenset("""
a about after again all also am an and any are as at be because been before being but by can
could did do does doing down for from had has have having he her here hers him his how i if in
into is it its just me more most my myself no nor not of off on once only or other our out over
own same she should so some such than that the their them then there these they this those
through to too under until up very was we were what when where which while who whom why will
with would you your yours near
yesterday today tomorrow last week weeks month months year years ago ever tell journal entry
entries wrote write written happen happened happening mention mentioned say said
""".split())

MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
_MONTH_WORDS = frozenset(MONTHS)

MOOD_WORDS = {
    "good": "good", "great": "good", "happy": "good",
    "okay": "okay", "ok": "okay", "fine": "okay",
    "rough": "rough",
    "low": "low", "down": "low",
    "sad": "sad", "unhappy": "sad",
}

# Month words that are also ordinary words: months only when capitalised, followed by a year or
# preceded by one of _MONTH_LEAD ("what may have helped" is not a date filter).
_AMBIGUOUS_MONTHS = frozenset({"may", "march", "mar"})
_MONTH_LEAD = frozenset({"in", "of", "during"})

MAX_TOKENS = 30
_TOKEN = re.compile(r"\w+", re.UNICODE)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


@dataclass(frozen=True)
class Hints:
    date_from: str | None = None
    date_to: str | None = None
    moods: tuple[str, ...] = ()


def build_query(question: str) -> str | None:
    seen: list[str] = []
    text = _ISO.sub(" ", (question or "").lower())
    for token in _TOKEN.findall(text.replace("_", " ")):
        if len(token) < 2 or token in STOP_WORDS or token in _MONTH_WORDS:
            continue
        if _YEAR.fullmatch(token):
            continue
        if token not in seen:
            seen.append(token)
            if len(seen) >= MAX_TOKENS:
                break
    if not seen:
        return None
    return " OR ".join(f'"{token}"' for token in seen)


def _month_range(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _is_month_use(original: list[str], i: int) -> bool:
    word = original[i]
    if word[:1].isupper():
        return True
    if i + 1 < len(original) and _YEAR.fullmatch(original[i + 1]):
        return True
    return i > 0 and original[i - 1].lower() in _MONTH_LEAD


def _date_range(question: str, today: date) -> tuple[date, date] | None:
    q = (question or "").lower()
    iso = _ISO.search(q)
    if iso:
        try:
            day = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            return day, day
        except ValueError:
            pass
    original = _TOKEN.findall(_ISO.sub(" ", question or ""))
    words = [w.lower() for w in original]
    text = " ".join(words)
    year_match = _YEAR.search(text)
    year = int(year_match.group(1)) if year_match else None
    for i, word in enumerate(words):
        month = MONTHS.get(word)
        if month and word in _AMBIGUOUS_MONTHS and not _is_month_use(original, i):
            continue
        if month:
            if year is not None:
                return _month_range(year, month)
            use_year = today.year if month <= today.month else today.year - 1
            return _month_range(use_year, month)
    if year is not None:
        return date(year, 1, 1), date(year, 12, 31)
    if "yesterday" in words:
        day = today - timedelta(days=1)
        return day, day
    if "today" in words:
        return today, today
    if "last week" in text:
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if "this week" in text:
        return today - timedelta(days=today.weekday()), today
    if "last month" in text:
        prev = today.replace(day=1) - timedelta(days=1)
        return _month_range(prev.year, prev.month)
    if "this month" in text:
        return today.replace(day=1), today
    return None


def parse_hints(question: str, today: date) -> Hints:
    span = _date_range(question, today)
    moods: list[str] = []
    for word in _TOKEN.findall((question or "").lower()):
        mood = MOOD_WORDS.get(word)
        if mood and mood not in moods:
            moods.append(mood)
    return Hints(
        date_from=span[0].isoformat() if span else None,
        date_to=span[1].isoformat() if span else None,
        moods=tuple(moods),
    )


def _run(conn, match, hints: Hints, use_dates: bool, use_moods: bool, limit: int):
    where, params = [], []
    sql = "SELECT e.* FROM entries e"
    order = "e.local_date DESC, e.id DESC"
    if match is not None:
        sql = "SELECT e.* FROM entries_fts JOIN entries e ON e.id = entries_fts.rowid"
        where.append("entries_fts MATCH ?")
        params.append(match)
        order = "bm25(entries_fts), " + order
    if use_dates and hints.date_from:
        where.append("e.local_date BETWEEN ? AND ?")
        params += [hints.date_from, hints.date_to]
    if use_moods and hints.moods:
        where.append("e.mood IN (%s)" % ",".join("?" * len(hints.moods)))
        params += list(hints.moods)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def find_entries(conn: sqlite3.Connection, question: str, today: date, limit: int = 8) -> list[sqlite3.Row]:
    limit = max(1, min(50, limit))
    match = build_query(question)
    hints = parse_hints(question, today)
    if match is None and not hints.date_from:
        return []
    if not (hints.date_from or hints.moods):
        return _run(conn, match, hints, False, False, limit)
    rows = _run(conn, match, hints, True, True, limit)
    if not rows and hints.date_from and hints.moods:
        rows = _run(conn, match, hints, True, False, limit)
    if match is None or len(rows) >= limit:
        return rows
    seen = {r["id"] for r in rows}
    for extra in _run(conn, match, hints, False, False, limit + len(rows)):
        if len(rows) >= limit:
            break
        if extra["id"] not in seen:
            rows.append(extra)
    return rows


RRF_K = 60  # reciprocal rank fusion constant: the usual value, flattens the two lists' ranks
LINKED_EXTRA = 2  # confirmed-linked entries added after the fused list


def _linked(conn: sqlite3.Connection, ids: list[int], have: set, limit: int) -> list[int]:
    """Entries one confirmed link away from the best results (story follow-ups), best first."""
    out: list[int] = []
    for entry_id in ids:
        for r in conn.execute(
            "SELECT from_entry_id AS a, to_entry_id AS b FROM entry_links"
            " WHERE status = 'confirmed' AND (from_entry_id = ? OR to_entry_id = ?) ORDER BY id",
            (entry_id, entry_id),
        ):
            other = r["b"] if r["a"] == entry_id else r["a"]
            if other not in have and other not in out:
                out.append(other)
                if len(out) >= limit:
                    return out
    return out


def find_entries_hybrid(conn: sqlite3.Connection, question: str, today: date, limit: int = 8,
                        qvec=None) -> list[sqlite3.Row]:
    """Keyword search and meaning search fused by rank, then entries linked to the best hits.

    Without a question vector (no key, embedding failed) this is exactly `find_entries`."""
    limit = max(1, min(50, limit))
    keyword = find_entries(conn, question, today, min(50, limit * 2))
    if qvec is None:
        return keyword[:limit]
    hints = parse_hints(question, today)
    meaning = [i for i, _ in embeddings.search_entries(conn, qvec, limit * 2, hints.date_from, hints.date_to)]
    if hints.date_from and len(meaning) < limit:  # like the keyword search: fill from outside the dates
        meaning += [i for i, _ in embeddings.search_entries(conn, qvec, limit * 2) if i not in meaning]
    if not meaning:
        return keyword[:limit]
    score: dict[int, float] = {}
    for ranked in ([r["id"] for r in keyword], meaning):
        for rank, entry_id in enumerate(ranked):
            score[entry_id] = score.get(entry_id, 0.0) + 1.0 / (RRF_K + rank + 1)
    order = sorted(score, key=lambda i: -score[i])[:limit]
    order += _linked(conn, order[:3], set(order), LINKED_EXTRA)
    rows = {r["id"]: r for r in keyword}
    missing = [i for i in order if i not in rows]
    if missing:
        marks = ",".join("?" * len(missing))
        for r in conn.execute(f"SELECT * FROM entries WHERE id IN ({marks})", missing):
            rows[r["id"]] = r
    return [rows[i] for i in order if i in rows]
