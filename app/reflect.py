import json
import re
import unicodedata
from datetime import datetime, timezone

from . import extract, focus, memory, periods, prompts, reflections
from .llm import LLMError
from .periods import Period

MAX_FOCUS = 3
MIN_QUOTE = 15
MAX_CALLS = 3
FIRST_TOKENS = 8192
RETRY_TOKENS = 12288
VERDICTS = ("progressing", "stalled", "failing")

_WS = re.compile(r"\s+")
_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,16}$")


def _norm(text: str) -> str:
    return _WS.sub(" ", text).strip()


_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                       "–": "-", "—": "-"})


def _cmp(text: str) -> str:
    """Comparison form only: NFC, curly quotes and dashes folded, whitespace collapsed."""
    return _norm(unicodedata.normalize("NFC", text).translate(_FOLD))


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if v.isdecimal():
            try:
                return int(v)
            except ValueError:
                return None
    return None


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _match_quote(quote: str, entry_text: str) -> str | None:
    """Return the verified (normalised) quote, or None. Tries the as-given and un-escaped forms."""
    entry_norm = _cmp(entry_text)
    for variant in dict.fromkeys((quote, prompts.unescape(quote))):
        q = _norm(variant)
        qc = _cmp(variant)
        if not qc:
            continue
        if len(qc) < MIN_QUOTE:
            if qc == entry_norm:
                return q
            continue
        if qc in entry_norm:
            return q
    return None


def verify_quotes(content: dict, entries) -> tuple[dict, list[str]]:
    """Keep only claims whose quote is verbatim in the cited entry. Returns (cleaned, dropped ids)."""
    by_id = {e["id"]: e for e in entries}
    content = content if isinstance(content, dict) else {}
    dropped: list[str] = []
    used: set[str] = set()
    counters = {"w": 0, "t": 0}

    # Model-supplied ids that are valid and unique are kept; the rest are assigned by code.
    def claim_id(item: dict, prefix: str) -> str:
        cid = item.get("id")
        if isinstance(cid, str) and _ID_OK.match(cid) and cid not in used:
            used.add(cid)
            return cid
        while True:
            counters[prefix] += 1
            cid = f"{prefix}{counters[prefix]}"
            if cid not in used:
                used.add(cid)
                return cid

    out = {"wins": [], "tough_love": [], "focus": [], "focus_review": []}
    for key, prefix in (("wins", "w"), ("tough_love", "t")):
        for item in _list(content.get(key)):
            if not isinstance(item, dict):
                continue
            cid = claim_id(item, prefix)
            text = _norm(_str(item.get("text")))
            entry = by_id.get(_int(item.get("entry_id")))
            quote = _match_quote(_str(item.get("quote")), entry["text"]) if entry is not None else None
            if not text or quote is None:
                dropped.append(cid)
                continue
            out[key].append({"id": cid, "text": text, "quote": quote,
                             "entry_id": entry["id"], "date": entry["local_date"]})
    for item in _list(content.get("focus")):
        text = _norm(_str(item.get("text"))) if isinstance(item, dict) else ""
        if text:
            out["focus"].append({"text": text})
    for idx, item in enumerate(_list(content.get("focus_review")), start=1):
        if not isinstance(item, dict):
            continue
        fid = _int(item.get("focus_id"))
        label = f"fr{fid if fid is not None else idx}"
        entry = by_id.get(_int(item.get("entry_id")))
        quote = _match_quote(_str(item.get("quote")), entry["text"]) if entry is not None else None
        if fid is None or item.get("verdict") not in VERDICTS or quote is None:
            dropped.append(label)
            continue
        out["focus_review"].append({"focus_id": fid, "verdict": item["verdict"], "quote": quote,
                                    "entry_id": entry["id"], "date": entry["local_date"]})
    return out, dropped


def _offered(raw) -> int:
    raw = raw if isinstance(raw, dict) else {}
    return sum(
        1
        for key in ("wins", "tough_love", "focus_review")
        for item in _list(raw.get(key))
        if isinstance(item, dict)
    )


def _verified(cleaned: dict) -> int:
    return len(cleaned["wins"]) + len(cleaned["tough_love"]) + len(cleaned["focus_review"])


def _ask(llm, system: str, user: str, calls_left: int, max_tokens: int):
    """One call, plus one larger-budget retry on truncation if the call budget allows.
    Returns (raw, calls_used, max_tokens_used)."""
    try:
        return llm.complete_json(system, user, max_tokens=max_tokens), 1, max_tokens
    except LLMError as exc:
        if exc.reason != "truncated" or calls_left < 2 or max_tokens >= RETRY_TOKENS:
            raise
    return llm.complete_json(system, user, max_tokens=RETRY_TOKENS), 2, RETRY_TOKENS


def _model_name(llm) -> str:
    answered = getattr(llm, "last_model", None)
    if answered:
        return str(answered)
    return str(getattr(getattr(llm, "_settings", None), "model", None) or "unknown")


_KINDS = ("link", "risk", "strength")
_OUTCOMES = ("helped", "worse", "neutral")
_CAME_TRUE = ("yes", "no", "unclear")


def _short(value, limit: int = 300) -> str:
    return _norm(_str(value))[:limit]


def _lines(value, limit: int) -> list[str]:
    out = []
    for item in _list(value):
        text = _short(item, 240) if isinstance(item, str) else ""
        if text:
            out.append(text)
    return out[:limit]


def _count(value) -> int:
    n = _int(value)
    return 1 if n is None or n < 1 else min(n, 99)


def _pick(value, allowed: tuple, default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


_SINCE = ("done", "ongoing", "not_done", "came_true", "did_not_happen", "unclear")


def _since_last(value, limit: int = 8) -> list[dict]:
    out = []
    for item in _list(value):
        text = _short(item.get("item"), 200) if isinstance(item, dict) else ""
        if text:
            out.append({"item": text, "status": _pick(item.get("status"), _SINCE, "unclear"),
                        "note": _short(item.get("note"), 300)})
    return out[:limit]


def _evidence(item: dict, by_id: dict) -> list[dict]:
    """A pattern's quotes that are word for word in the entry they name (at most 2)."""
    out = []
    for ev in _list(item.get("evidence")):
        if not isinstance(ev, dict):
            continue
        entry = by_id.get(_int(ev.get("entry_id")))
        quote = _match_quote(_str(ev.get("quote")), entry["text"]) if entry is not None else None
        if quote is not None:
            out.append({"quote": quote, "entry_id": entry["id"], "date": entry["local_date"]})
    return out[:2]


def previous_report(conn, kind: str, period) -> dict | None:
    """Open items of the latest report of this kind that ended before `period` starts."""
    row = conn.execute(
        "SELECT period_key, content FROM reflections WHERE kind = ? AND is_latest = 1 AND period_end < ?"
        " ORDER BY period_end DESC, version DESC LIMIT 1",
        (kind, period.start.isoformat()),
    ).fetchone()
    if row is None:
        return None
    try:
        content = json.loads(row["content"])
    except ValueError:
        return None
    if not isinstance(content, dict):
        return None
    focus_items = content.get("focus") if kind == "weekly" else content.get("focus_next")
    return {"label": row["period_key"],
            "improve": [i for i in _list(content.get("improve")) if isinstance(i, dict)][:5],
            "worries": [w for w in _list(content.get("worries")) if isinstance(w, dict)][:5],
            "focus": [f for f in _list(focus_items) if isinstance(f, dict)][:5]}


def _patterns(value, limit: int, weeks: set | None = None, by_id: dict | None = None) -> list[dict]:
    out = []
    for item in _list(value):
        if not isinstance(item, dict):
            continue
        text = _short(item.get("text"), 400)
        if not text:
            continue
        row = {"label": _short(item.get("label"), 80) or text[:60],
               "kind": _pick(item.get("kind"), _KINDS, "link"), "text": text}
        if weeks is not None:
            keep = list(dict.fromkeys(w for w in _list(item.get("weeks")) if isinstance(w, str) and w in weeks))
            if not keep:
                continue
            row["weeks"] = keep
        if by_id is not None:
            row["evidence"] = _evidence(item, by_id)
            row["backed"] = bool(row["evidence"])
        out.append(row)
    return out[:limit]


def _improve(value, limit: int) -> list[dict]:
    out = []
    for item in _list(value):
        step = _short(item.get("step"), 300) if isinstance(item, dict) else ""
        if step:
            out.append({"step": step, "why": _short(item.get("why"), 300)})
    return out[:limit]


def _worries(value, limit: int, weeks: set | None = None) -> list[dict]:
    out = []
    for item in _list(value):
        worry = _short(item.get("worry"), 200) if isinstance(item, dict) else ""
        if not worry:
            continue
        row = {"worry": worry, "times": _count(item.get("times")),
               "came_true": _pick(item.get("came_true"), _CAME_TRUE, "unclear"),
               "note": _short(item.get("note"), 300)}
        if weeks is not None:
            row["weeks"] = [w for w in _list(item.get("weeks")) if isinstance(w, str) and w in weeks]
        out.append(row)
    return out[:limit]


def day_series(entries) -> list[dict]:
    """Per local day: last mood written and average energy. Computed from the entries, never by the model."""
    days: dict = {}
    for e in entries:
        d = days.setdefault(e["local_date"], {"mood": None, "energy": []})
        if e["mood"]:
            d["mood"] = e["mood"]
        if e["energy_score"] is not None:
            d["energy"].append(e["energy_score"])
    return [{"date": k, "mood": v["mood"],
             "energy": round(sum(v["energy"]) / len(v["energy"])) if v["energy"] else None}
            for k, v in sorted(days.items())]


def weekly_extra(raw, entries) -> dict:
    """The detailed report sections. Triggers need a verbatim quote like wins do; the rest is the model's wording."""
    raw = raw if isinstance(raw, dict) else {}
    by_id = {e["id"]: e for e in entries}
    triggers = []
    for item in _list(raw.get("triggers")):
        if not isinstance(item, dict):
            continue
        entry = by_id.get(_int(item.get("entry_id")))
        quote = _match_quote(_str(item.get("quote")), entry["text"]) if entry is not None else None
        trigger = _short(item.get("trigger"), 200)
        if quote is None or not trigger:
            continue
        triggers.append({"trigger": trigger, "felt": _short(item.get("felt"), 200),
                         "did": _short(item.get("did"), 200),
                         "outcome": _pick(item.get("outcome"), _OUTCOMES, "neutral"),
                         "quote": quote, "entry_id": entry["id"], "date": entry["local_date"]})
    return {
        "v": 2,
        "summary_lines": _lines(raw.get("summary"), 5),
        "overall_feel": _short(raw.get("overall_feel"), 600),
        "patterns": _patterns(raw.get("patterns"), 8, by_id=by_id),
        "since_last": _since_last(raw.get("since_last")),
        "triggers": triggers[:5],
        "worries": _worries(raw.get("worries"), 5),
        "improve": _improve(raw.get("improve"), 3),
        "questions": _lines(raw.get("questions"), 3),
        "takeaway": _short(raw.get("takeaway"), 300),
        "by_day": day_series(entries),
    }


def monthly_extra(raw, week_keys: list[str], entries) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    allowed = set(week_keys)
    triggers = []
    for item in _list(raw.get("triggers")):
        trigger = _short(item.get("trigger"), 200) if isinstance(item, dict) else ""
        if not trigger:
            continue
        triggers.append({"trigger": trigger, "felt": _short(item.get("felt"), 200),
                         "did": _short(item.get("did"), 200),
                         "outcome": _pick(item.get("outcome"), _OUTCOMES, "neutral"),
                         "count": _count(item.get("count")),
                         "weeks": [w for w in _list(item.get("weeks")) if isinstance(w, str) and w in allowed]})
    recap, seen = [], set()
    for item in _list(raw.get("recap")):
        week = item.get("week") if isinstance(item, dict) else None
        text = _short(item.get("text"), 400) if isinstance(item, dict) else ""
        if isinstance(week, str) and week in allowed and week not in seen and text:
            seen.add(week)
            recap.append({"week": week, "text": text})
    return {
        "v": 2,
        "summary_lines": _lines(raw.get("summary"), 10),
        "triggers": triggers[:10],
        "worries": _worries(raw.get("worries"), 8, allowed),
        "recap": recap,
        "focus_progress": _short(raw.get("focus_progress"), 800),
        "hidden_wins": _lines(raw.get("hidden_wins"), 4),
        "improve": _improve(raw.get("improve"), 5),
        "questions": _lines(raw.get("questions"), 5),
        "by_day": day_series(entries),
    }


def generate_weekly(conn, llm, period: Period, now: datetime) -> int | None:
    entries = periods.entries_for(conn, period)
    if not entries:
        return None
    short = len(entries) < 3
    active = [f for f in focus.list_all(conn) if f["status"] in ("active", "failing")]
    system = prompts.weekly_system()
    counted = extract.numbers(conn, entries)
    user = prompts.weekly_user(entries, memory.list_active(conn), active, short,
                               previous=previous_report(conn, "weekly", period), counted=counted)

    live_ids = {f["id"] for f in active}
    status_by_id = {f["id"]: f["status"] for f in active}

    def check(raw_):
        cleaned_, dropped_ = verify_quotes(raw_, entries)
        cleaned_["focus_review"] = [r for r in cleaned_["focus_review"] if r["focus_id"] in live_ids]
        return cleaned_, dropped_

    raw, used, tokens = _ask(llm, system, user, MAX_CALLS, FIRST_TOKENS)
    chosen = raw
    cleaned, dropped = check(raw)
    offered = _offered(raw)
    if offered >= 2 and len(dropped) / offered > 0.5 and used < MAX_CALLS:
        try:
            raw2, _, _ = _ask(llm, system, user + "\n\n" + prompts.weekly_retry_note(),
                              MAX_CALLS - used, tokens)
        except LLMError:
            if _verified(cleaned) == 0:
                raise  # nothing usable yet: let the job runner retry or fail the job
            raw2 = None  # keep the first, already usable, result
        if raw2 is not None:
            cleaned2, dropped2 = check(raw2)
            if _verified(cleaned2) > _verified(cleaned):
                cleaned, dropped = cleaned2, dropped2
                chosen = raw2

    unverified = _verified(cleaned) == 0
    if unverified:
        cleaned = {"wins": [], "tough_love": [], "focus": [], "focus_review": []}

    existing = {_norm(f["text"]): f["id"] for f in active}
    with conn:  # one transaction: reflection, focus rows, links and status changes all or nothing
        focus_out, new_ids = [], []
        for item in cleaned["focus"][:MAX_FOCUS]:
            key = _norm(item["text"])
            if key in existing:
                fid = existing[key]
            else:
                fid = focus.add(conn, item["text"], None, now, commit=False)
                existing[key] = fid
                new_ids.append(fid)
            focus_out.append({"id": fid, "text": item["text"]})

        content = {
            "wins": cleaned["wins"],
            "tough_love": cleaned["tough_love"],
            "focus": focus_out,
            "focus_review": cleaned["focus_review"],
            "short": short,
            "dropped": dropped,
            "unverified": unverified,
            "entry_count": len(entries),  # what the model saw, fixed at generation time
            "covers_through": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **weekly_extra(chosen, entries),
            "numbers": counted,
        }
        rid = reflections.save(
            conn, kind="weekly", period_key=period.key,
            period_start=period.start.isoformat(), period_end=period.end.isoformat(),
            model=_model_name(llm), content=content, now=now, commit=False,
        )
        for fid in new_ids:
            focus.set_source(conn, fid, rid, commit=False)
        for review in cleaned["focus_review"]:
            if review["verdict"] == "failing" and status_by_id[review["focus_id"]] == "active":
                focus.set_status(conn, review["focus_id"], "failing", now, commit=False)
    return rid


MAX_PATTERNS = 12
MAX_NEXT = 5
MONTHLY_CALLS = 2


def _weeks_in_month(conn, period: Period) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM reflections WHERE kind = 'weekly' AND is_latest = 1"
        " AND period_end BETWEEN ? AND ? ORDER BY period_end, id",
        (period.start.isoformat(), period.end.isoformat()),
    ).fetchall()
    weeks = []
    for row in rows:
        content = reflections.content(row)
        if not isinstance(content, dict) or content.get("unverified"):
            continue
        weeks.append({
            "key": row["period_key"], "start": row["period_start"], "end": row["period_end"],
            "wins": [c for c in _list(content.get("wins")) if isinstance(c, dict)],
            "tough_love": [c for c in _list(content.get("tough_love")) if isinstance(c, dict)],
            "focus": [f for f in _list(content.get("focus")) if isinstance(f, dict)],
            "summary_lines": _lines(content.get("summary_lines"), 5),
            "patterns": [c for c in _list(content.get("patterns")) if isinstance(c, dict)],
            "triggers": [c for c in _list(content.get("triggers")) if isinstance(c, dict)],
            "worries": [c for c in _list(content.get("worries")) if isinstance(c, dict)],
        })
    return weeks


def _clean_monthly(raw, week_keys: list[str]) -> tuple[dict, list[str]]:
    raw = raw if isinstance(raw, dict) else {}
    allowed = set(week_keys)
    dropped: list[str] = []
    used: set[str] = set()
    counter = 0

    def pattern_id(item: dict) -> str:
        nonlocal counter
        pid = item.get("id")
        if isinstance(pid, str) and _ID_OK.match(pid) and pid not in used:
            used.add(pid)
            return pid
        while True:
            counter += 1
            pid = f"p{counter}"
            if pid not in used:
                used.add(pid)
                return pid

    patterns = []
    for item in _list(raw.get("patterns")):
        if not isinstance(item, dict):
            continue
        pid = pattern_id(item)
        text = _norm(_str(item.get("text")))
        weeks = list(dict.fromkeys(w for w in _list(item.get("weeks")) if isinstance(w, str) and w in allowed))
        if not text or not weeks:
            dropped.append(pid)
            continue
        patterns.append({"id": pid, "text": text, "weeks": weeks,
                         "label": _short(item.get("label"), 80) or text[:60],
                         "kind": _pick(item.get("kind"), _KINDS, "link")})
    patterns = patterns[:MAX_PATTERNS]

    focus_next = []
    for item in _list(raw.get("focus_next")):
        text = _norm(_str(item.get("text"))) if isinstance(item, dict) else ""
        if text:
            focus_next.append({"text": text})
    summary = raw.get("summary")
    if isinstance(summary, list):
        summary = " ".join(_lines(summary, 10))
    return {"summary": _norm(_str(summary)), "patterns": patterns,
            "focus_next": focus_next[:MAX_NEXT]}, dropped


def generate_monthly(conn, llm, period: Period, now: datetime) -> int | None:
    weeks = _weeks_in_month(conn, period)
    if not weeks:
        return None
    week_keys = [w["key"] for w in weeks]
    short = len(weeks) < 2
    active = [f for f in focus.list_all(conn) if f["status"] in ("active", "failing")]
    system = prompts.monthly_system()
    month_entries = periods.entries_for(conn, period)
    counted = extract.numbers(conn, month_entries)
    user = prompts.monthly_user(weeks, memory.list_active(conn), active, short,
                                previous=previous_report(conn, "monthly", period), counted=counted,
                                facts=extract.digest(conn, month_entries))

    raw, _, _ = _ask(llm, system, user, MONTHLY_CALLS, FIRST_TOKENS)
    cleaned, dropped = _clean_monthly(raw, week_keys)
    unverified = not cleaned["patterns"] and not cleaned["summary"]
    if unverified:
        cleaned = {"summary": "", "patterns": [], "focus_next": []}
    extra = monthly_extra(raw, week_keys, month_entries)
    extra["since_last"] = _since_last(raw.get("since_last") if isinstance(raw, dict) else None)
    extra["numbers"] = counted
    content = {**cleaned, **extra, "weeks_used": week_keys, "short": short,
               "dropped": dropped, "unverified": unverified}
    with conn:  # single transaction, matching generate_weekly
        return reflections.save(
            conn, kind="monthly", period_key=period.key,
            period_start=period.start.isoformat(), period_end=period.end.isoformat(),
            model=_model_name(llm), content=content, now=now, commit=False,
        )
