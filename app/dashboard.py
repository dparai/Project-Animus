"""View-models for the Reflections page.

Presentation only. Model-written text is passed through untouched and escaped by the templates
(autoescape on, no `|safe`). Job errors are mapped to fixed phrases: raw `last_error` values are
never shown.
"""
import re
import sqlite3
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import focus, jobs, memory, periods, reflections, stats, tz
from app.moods import MOODS

NO_KEY_HINT = "Add LLM_API_KEY to the server's .env"
NO_KEY_STATE = {"text": "No API key configured.", "tone": "warn", "poll": False, "hint": NO_KEY_HINT}
STILL_WORKING_STATE = {"text": "Still working. Reload the page to check.", "tone": "warn", "poll": False, "hint": ""}
MAX_POLLS = 60  # 60 polls x 3 s = about 3 minutes
_STAMP = "%Y-%m-%dT%H:%M:%SZ"

# Chart geometry in px; the SVG itself stretches to the panel width (x values are percentages).
PLOT_TOP = 8
PLOT_HEIGHT = 100
BASELINE = PLOT_TOP + PLOT_HEIGHT
CHART_HEIGHT = 132


def _list(value) -> list:
    return value if isinstance(value, list) else []


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def short_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def day_month(d: date) -> str:
    return f"{d.day} {d:%b}"


def _iso(value) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _week_start(key) -> date | None:
    try:
        year, _, week = _str(key).partition("-W")
        return date.fromisocalendar(int(year), int(week), 1)
    except (TypeError, ValueError):
        return None


def _generated(row, zone: str) -> str:
    utc = datetime.strptime(row["created_at"], _STAMP).replace(tzinfo=timezone.utc)
    local = utc.astimezone(ZoneInfo(zone))
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"


def _unique_id(raw, rid: int, prefix: str, index: int, seen: set) -> str:
    """The stored id when it is usable and unique, else a stable fallback from reflection, section, index."""
    cid = raw.strip() if isinstance(raw, str) else ""
    if not cid or cid in seen:
        cid = f"{rid}-{prefix}-{index}"
    seen.add(cid)
    return cid


def _claims(items, rid: int, prefix: str, seen: set) -> list[dict]:
    out = []
    for index, item in enumerate(_list(items)):
        item = _dict(item)
        day = _iso(item.get("date"))
        out.append({
            "id": _unique_id(item.get("id"), rid, prefix, index, seen),
            "text": _str(item.get("text")),
            "quote": _str(item.get("quote")),
            "when": short_day(day) if day else "",
            "iso": day.isoformat() if day else "",
        })
    return out


def _weekly_claims(content: dict, rid: int) -> tuple[list[dict], list[dict]]:
    """Wins and tough love with page-unique ids (one `seen` set across both sections)."""
    seen: set = set()
    return _claims(content.get("wins"), rid, "w", seen), _claims(content.get("tough_love"), rid, "t", seen)


def _patterns(content: dict, rid: int) -> list[dict]:
    out = []
    seen: set = set()
    for index, item in enumerate(_list(content.get("patterns"))):
        item = _dict(item)
        weeks = [day_month(d) for d in (_week_start(k) for k in _list(item.get("weeks"))) if d]
        kind = item.get("kind")
        out.append({"id": _unique_id(item.get("id"), rid, "p", index, seen), "text": _str(item.get("text")), "weeks": weeks,
                    "label": _str(item.get("label")) or _str(item.get("text"))[:60],
                    "kind": kind if kind in ("link", "risk", "strength") else "link"})
    return out


def claim_index(content: dict, reflection_id: int) -> dict[str, dict]:
    """Displayed claim id -> {"text", "section"} for everything the owner can correct.

    Built from the same helpers the page renders from, so a correction always resolves the claim the
    owner was looking at, fallback ids included. Focus items and summaries are not correctable.
    """
    content = _dict(content)
    wins, tough = _weekly_claims(content, reflection_id)
    index = {c["id"]: {"text": c["text"], "section": "wins"} for c in wins}
    index.update({c["id"]: {"text": c["text"], "section": "tough_love"} for c in tough})
    index.update({p["id"]: {"text": p["text"], "section": "patterns"} for p in _patterns(content, reflection_id)})
    return index


def weekly_view(conn: sqlite3.Connection, zone: str) -> dict | None:
    row = reflections.latest(conn, "weekly")
    return None if row is None else weekly_view_row(row, zone)


def weekly_view_row(row, zone: str) -> dict:
    content = _dict(reflections.content(row))
    start, end = _iso(row["period_start"]), _iso(row["period_end"])
    wins, tough_love = _weekly_claims(content, row["id"])
    stored = content.get("entry_count")  # fixed at generation; older rows have none
    count = stored if isinstance(stored, int) and not isinstance(stored, bool) and stored >= 0 else None
    return {
        "kind": "weekly",
        "id": row["id"],
        "title": f"Week of {day_month(start)}" if start else "Weekly reflection",
        "range": f"{day_month(start)} to {day_month(end)}" if start and end else "",
        "entry_count": count,
        "generated": _generated(row, zone),
        "unverified": bool(content.get("unverified")),
        "short": bool(content.get("short")),
        "wins": wins,
        "tough_love": tough_love,
        "focus": [_str(_dict(f).get("text")) for f in _list(content.get("focus")) if _str(_dict(f).get("text"))],
        "v2": _v2(content, start, end, True) if content.get("v") == 2 else None,
    }


def monthly_view(conn: sqlite3.Connection, zone: str) -> dict | None:
    row = reflections.latest(conn, "monthly")
    return None if row is None else monthly_view_row(row, zone)


def monthly_view_row(row, zone: str) -> dict:
    content = _dict(reflections.content(row))
    start = _iso(row["period_start"])
    end = _iso(row["period_end"])
    return {
        "kind": "monthly",
        "range": f"{day_month(start)} to {day_month(end)}" if start and end else "",
        "entry_count": None,
        "id": row["id"],
        "title": f"{start:%B %Y}" if start else "Monthly reflection",
        "generated": _generated(row, zone),
        "unverified": bool(content.get("unverified")),
        "short": bool(content.get("short")),
        "summary": _str(content.get("summary")),
        "patterns": _patterns(content, row["id"]),
        "focus_next": [_str(_dict(f).get("text")) for f in _list(content.get("focus_next")) if _str(_dict(f).get("text"))],
        "v2": _v2(content, start, end, False) if content.get("v") == 2 else None,
    }


_OUT = ("helped", "worse", "neutral")
_TRUE = ("yes", "no", "unclear")


def _lines(value) -> list[str]:
    return [t for t in (_str(x) for x in _list(value)) if t]


def _bars(by_day, start: date | None, end: date | None, weekly: bool) -> list[dict]:
    """One bar per local day from stored by_day (computed from the entries, not by the AI)."""
    if start is None or end is None:
        return []
    mapping = {_str(_dict(d).get("date")): _dict(d) for d in _list(by_day)}
    out, day = [], start
    while day <= end and len(out) < 31:
        item = mapping.get(day.isoformat(), {})
        energy = item.get("energy") if isinstance(item.get("energy"), int) and not isinstance(item.get("energy"), bool) else None
        mood = _str(item.get("mood"))
        title = short_day(day)
        if energy:
            title += f", energy {energy}/10"
        if mood:
            title += f", mood {mood}"
        if not item:
            title += ", no entry"
        label = "MTWTFSS"[day.weekday()] if weekly else (str(day.day) if day.day in (1, 8, 15, 22, 29) else "")
        out.append({"h": round(energy * 4.5) if energy else 4, "has": bool(energy), "title": title, "label": label})
        day = date.fromordinal(day.toordinal() + 1)
    return out


def _v2(content: dict, start, end, weekly: bool) -> dict:
    triggers = []
    for t in _list(content.get("triggers")):
        t = _dict(t)
        day = _iso(t.get("date"))
        outcome = t.get("outcome")
        triggers.append({
            "trigger": _str(t.get("trigger")), "felt": _str(t.get("felt")), "did": _str(t.get("did")),
            "outcome": outcome if outcome in _OUT else "neutral", "quote": _str(t.get("quote")),
            "when": short_day(day) if day else "",
            "count": t.get("count") if isinstance(t.get("count"), int) and not isinstance(t.get("count"), bool) else None,
        })
    worries = []
    for w in _list(content.get("worries")):
        w = _dict(w)
        times = w.get("times")
        came = w.get("came_true")
        worries.append({"worry": _str(w.get("worry")),
                        "times": times if isinstance(times, int) and not isinstance(times, bool) else 1,
                        "came_true": came if came in _TRUE else "unclear", "note": _str(w.get("note"))})
    patterns = []
    for p in _list(content.get("patterns")):
        p = _dict(p)
        kind = p.get("kind")
        patterns.append({"label": _str(p.get("label")), "text": _str(p.get("text")),
                         "kind": kind if kind in ("link", "risk", "strength") else "link"})
    recap = []
    for r in _list(content.get("recap")):
        r = _dict(r)
        d = _week_start(r.get("week"))
        recap.append({"label": f"Week of {day_month(d)}" if d else _str(r.get("week")), "text": _str(r.get("text"))})
    improve = [{"step": _str(_dict(i).get("step")), "why": _str(_dict(i).get("why"))}
               for i in _list(content.get("improve")) if _str(_dict(i).get("step"))]
    return {
        "summary": _lines(content.get("summary_lines")),
        "overall_feel": _str(content.get("overall_feel")),
        "takeaway": _str(content.get("takeaway")),
        "patterns": patterns,
        "triggers": [t for t in triggers if t["trigger"]],
        "worries": [w for w in worries if w["worry"]],
        "improve": improve,
        "questions": _lines(content.get("questions")),
        "recap": recap,
        "focus_progress": _str(content.get("focus_progress")),
        "hidden_wins": _lines(content.get("hidden_wins")),
        "bars": _bars(content.get("by_day"), start, end, weekly),
    }


def history(conn: sqlite3.Connection) -> dict:
    """Every stored report per kind (latest version of each period), newest first, for the accordion lists."""
    out = {}
    for kind in ("weekly", "monthly"):
        rows = conn.execute(
            "SELECT id, period_start, period_end FROM reflections WHERE kind = ? AND is_latest = 1"
            " ORDER BY period_end DESC, id DESC LIMIT 60", (kind,),
        ).fetchall()
        items = []
        for i, r in enumerate(rows):
            s, e = _iso(r["period_start"]), _iso(r["period_end"])
            if not s or not e:
                continue
            if kind == "weekly":
                items.append({"id": r["id"], "label": f"{short_day(s)} to {short_day(e)}", "sub": "Latest" if i == 0 else ""})
            else:
                items.append({"id": r["id"], "label": f"{s:%B %Y}", "sub": f"{day_month(s)} to {day_month(e)}"})
        out[kind] = items
    return out


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
RIBBON_MAX = 80
PREVIEW_CHARS = 200


def _preview(text: str) -> str:
    flat = " ".join(text.split())
    parts = _SENTENCE.split(flat)
    out = " ".join(parts[:3])
    if len(out) > PREVIEW_CHARS:
        out = out[:PREVIEW_CHARS].rstrip() + "\u2026"
    elif len(parts) > 3:
        out += "\u2026"
    return out


def _entry_when(row, zone: str) -> str:
    try:
        utc = datetime.strptime(row["created_at"], _STAMP).replace(tzinfo=timezone.utc)
        local = utc.astimezone(ZoneInfo(zone))
        return f"{local:%a} {local.day} {local:%b}, {local:%-I:%M %p}"
    except (ValueError, TypeError):
        return str(row["local_date"])


def ribbon(conn: sqlite3.Connection, row, zone: str) -> list[dict]:
    entries = conn.execute(
        "SELECT id, created_at, local_date, text FROM entries WHERE local_date BETWEEN ? AND ?"
        " ORDER BY created_at, id LIMIT ?", (row["period_start"], row["period_end"], RIBBON_MAX),
    ).fetchall()
    return [{"id": e["id"], "when": _entry_when(e, zone), "preview": _preview(e["text"])} for e in entries]


def report_context(conn: sqlite3.Connection, row, zone: str) -> dict:
    view = weekly_view_row(row, zone) if row["kind"] == "weekly" else monthly_view_row(row, zone)
    return {"kind": row["kind"], "r": view, "ribbon": ribbon(conn, row, zone)}


_UNANSWERED = "the AI service did not answer"
_UNREADABLE = "the model's answer could not be read"
_REASONS = {  # fixed reason tokens from app.llm -> fixed phrases; anything else is generic
    "http_401": "the API key was rejected",
    "http_403": "the API key was rejected",
    "http_404": "the model name was not found",
    "truncated": "the model's answer was cut off",
    "no_json": _UNREADABLE,
    "bad_shape": _UNREADABLE,
    "timeout": _UNANSWERED,
    "unreachable": _UNANSWERED,
    "http_429": _UNANSWERED,
    "http_5xx": _UNANSWERED,
    "http_408": _UNANSWERED,
}


def _reason(last_error) -> str:
    return _REASONS.get(last_error, "something went wrong, try again")


def reflection_state(conn: sqlite3.Connection, kind: str, now: datetime) -> dict:
    """The one-line state for a kind: from its newest job, else from its latest reflection."""
    job = conn.execute(
        "SELECT * FROM reflection_jobs WHERE kind = ?"
        " ORDER BY COALESCE(NULLIF(queued_at, ''), created_at) DESC, id DESC LIMIT 1",
        (kind,),
    ).fetchone()
    status = job["status"] if job else None
    if status == "running":
        return {"text": "Generating... this can take a minute", "tone": "busy", "poll": True, "hint": ""}
    if status == "pending":
        if job["last_error"] in (jobs.NO_KEY_MESSAGE, "no_key"):
            return {"text": "Waiting for an API key", "tone": "warn", "poll": False, "hint": NO_KEY_HINT}
        stamp = now.astimezone(timezone.utc).strftime(_STAMP)
        retry = job["attempts"] > 0 or bool(job["next_attempt_at"] and job["next_attempt_at"] > stamp)
        return {"text": "Pending, will retry" if retry else "Generating... this can take a minute", "tone": "busy",
                "poll": True, "hint": ""}
    if status == "failed":
        return {"text": f"Failed: {_reason(job['last_error'])}", "tone": "bad", "poll": False,
                "hint": "Press Generate now to try again."}
    if status == "skipped":
        what = "entries in that week" if kind == "weekly" else "weekly reflections in that month"
        return {"text": "Nothing to reflect on", "tone": "neutral", "poll": False, "hint": f"There were no {what}."}
    row = reflections.latest(conn, kind)
    if row is None:
        return {"text": "No reflection yet", "tone": "neutral", "poll": False, "hint": ""}
    start = _iso(row["period_start"])
    if kind == "weekly":
        label = f"week of {day_month(start)}" if start else "latest week"
    else:
        label = f"{start:%B %Y}" if start else "latest month"
    return {"text": f"Last {kind} reflection: {label}", "tone": "neutral", "poll": False, "hint": ""}


def focus_view(conn: sqlite3.Connection, zone: str) -> dict:
    rows = focus.list_all(conn)
    items = []
    for r in rows:
        done_on = None
        if r["status"] == "done" and r["completed_at"]:
            utc = datetime.strptime(r["completed_at"], _STAMP).replace(tzinfo=timezone.utc)
            done_on = day_month(utc.astimezone(ZoneInfo(zone)).date())
        items.append({"id": r["id"], "text": r["text"], "status": r["status"],
                      "done": r["status"] == "done", "failing": r["status"] == "failing", "done_on": done_on})
    items.sort(key=lambda i: i["done"])  # stable: open items first, newest first within each group
    return {"rows": items, "open": sum(1 for i in items if not i["done"]),
            "done_count": sum(1 for i in items if i["done"])}


def chart_view(series: list[tuple[date, float | None]], today: date) -> dict:
    n = len(series)
    slot = 100 / n
    bars = []
    for i, (day, value) in enumerate(series):
        height = value * (PLOT_HEIGHT / 10) if value is not None else 0
        bars.append({
            "day": day.isoformat(),
            "long": short_day(day),
            "number": day.day,
            "value": None if value is None else f"{value:.1f}",
            "x": round(i * slot + slot * 0.14, 3),
            "w": round(slot * 0.72, 3),
            "cx": round((i + 0.5) * slot, 3),
            "y": round(BASELINE - height, 2),
            "h": round(height, 2),
            "is_today": day == today,
            "label": (n - 1 - i) % 2 == 0,  # every other day on phones; today is always labelled
        })
    have = [(d, v) for d, v in series if v is not None]
    first, last = series[0][0], series[-1][0]
    if have:
        hi = max(have, key=lambda p: p[1])
        lo = min(have, key=lambda p: p[1])
        summary = (f"{len(have)} of {n} days have an entry with energy. "
                   f"Highest {hi[1]:.1f} on {day_month(hi[0])}, lowest {lo[1]:.1f} on {day_month(lo[0])}.")
    else:
        summary = f"No energy was recorded in these {n} days."
    return {"bars": bars, "empty": not have, "range": f"{day_month(first)} to {day_month(last)}",
            "summary": summary}


def page_context(conn: sqlite3.Connection, now: datetime) -> dict:
    zone = tz.get_timezone(conn)
    local = now.astimezone(ZoneInfo(zone))
    today = local.date()
    s = stats.week_stats(conn, today, zone)
    return {
        "page": "reflections",
        "date_line": f"{local:%A} {local.day} {local:%B}",
        "stats": s,
        "avg_energy": None if s.avg_energy is None else f"{s.avg_energy:.1f}",
        "moods": [{"emoji": m.emoji, "label": m.label, "count": s.mood_counts.get(m.key, 0)} for m in MOODS],
        "chart": chart_view(s.energy_series, today),
        "baseline": BASELINE,
        "plot_top": PLOT_TOP,
        "plot_height": PLOT_HEIGHT,
        "chart_height": CHART_HEIGHT,
        "weekly": weekly_view(conn, zone),
        "monthly": monthly_view(conn, zone),
        "states": {k: reflection_state(conn, k, now) for k in jobs.KINDS},
        "focus": focus_view(conn, zone),
        "history": history(conn),
        "ribbons": {k: (ribbon(conn, reflections.latest(conn, k), zone) if reflections.latest(conn, k) else []) for k in ("weekly", "monthly")},
    }


MEMORY_SHOWN = 200
_KIND_LABEL = {"correction": "Correction", "note": "Note"}


def _conversation_starts(conn, ids) -> dict:
    """started_at per conversation id; deleted conversations are simply missing."""
    if not ids:
        return {}
    ids = sorted(ids)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(f"SELECT id, started_at FROM conversations WHERE id IN ({marks})", ids).fetchall()
    return {r["id"]: r["started_at"] for r in rows}


def memory_view(conn: sqlite3.Connection, zone: str) -> dict:
    """Active AI notes, newest first, capped at MEMORY_SHOWN for the Memory page."""
    rows = memory.list_all_active(conn, MEMORY_SHOWN + 1)
    shown = rows[:MEMORY_SHOWN]
    this_year = datetime.now(timezone.utc).astimezone(ZoneInfo(zone)).year
    started = _conversation_starts(conn, {r["conversation_id"] for r in shown if r["conversation_id"]})
    items = []
    for r in shown:
        utc = datetime.strptime(r["created_at"], _STAMP).replace(tzinfo=timezone.utc)
        local = utc.astimezone(ZoneInfo(zone))
        year = "" if local.year == this_year else f" {local.year}"
        source = None
        if r["conversation_id"] is not None:
            # Only the conversation's date is shown, never any of its message text.
            source = "from a conversation"
            begun = started.get(r["conversation_id"])
            if begun:
                day = datetime.strptime(begun, _STAMP).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone))
                day_year = "" if day.year == this_year else f" {day.year}"
                source += f", {day:%A} {day.day} {day:%B}{day_year}"
        items.append({
            "id": r["id"],
            "kind": _KIND_LABEL.get(r["kind"], "Note"),
            "when": f"{local:%A} {local.day} {local:%B}{year}, {local:%H:%M}",
            "iso": r["created_at"],
            "text": r["text"],
            "source": source,
        })
    sent = len(memory.list_active(conn))  # exactly what the AI receives: corrections first, then newest notes
    return {"notes": items, "capped": len(rows) > MEMORY_SHOWN, "shown": MEMORY_SHOWN,
            "beyond_prompt": len(rows) > sent, "prompt_limit": memory.PROMPT_LIMIT,
            "prompt_corrections": memory.PROMPT_CORRECTIONS, "sent": sent}
