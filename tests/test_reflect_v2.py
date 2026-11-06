import datetime

from app import db, periods, reflect, reflections
from tests.fake_llm import FakeLLM
from tests.test_dashboard import add_entry, today_local

TEXT = "I skipped lunch again and felt drained by evening, then scrolled for an hour."
QUOTE = "skipped lunch again and felt drained by evening"


def _setup(tmp_path):
    path = str(tmp_path / "v2.db")
    c = db.connect(path)
    db.init_db(c)
    c.close()
    return path


def _raw(eid):
    return {
        "wins": [], "tough_love": [], "focus": [], "focus_review": [],
        "summary": ["a", "b", "c", "d", "e", "f"],
        "overall_feel": "Tired week.",
        "patterns": [{"label": "Sleep", "kind": "risk", "text": "Late nights, low mood."},
                     {"label": "", "kind": "bogus", "text": "Walks help."}, {"text": ""}],
        "triggers": [
            {"trigger": "Skipped lunch", "felt": "Drained", "did": "Scrolled", "outcome": "worse",
             "quote": QUOTE, "entry_id": eid},
            {"trigger": "Invented", "felt": "x", "did": "y", "outcome": "helped",
             "quote": "this text is not in the entry at all", "entry_id": eid},
        ],
        "worries": [{"worry": "Falling behind", "times": 3, "came_true": "no", "note": "Finished early."},
                    {"worry": "Odd", "times": "x", "came_true": "maybe"}],
        "improve": [{"step": "Fix a bedtime", "why": "Best days follow early nights."}] * 4,
        "questions": ["Q1", "Q2", "Q3", "Q4"],
        "takeaway": "Meals matter.",
    }


def test_weekly_extra_cleans_and_verifies(tmp_path):
    path = _setup(tmp_path)
    eid = add_entry(path, today_local().isoformat(), text=TEXT)
    conn = db.connect(path)
    entries = periods.entries_for(conn, periods.week_of(today_local()))
    out = reflect.weekly_extra(_raw(eid), entries)
    assert out["v"] == 2 and out["summary_lines"] == ["a", "b", "c", "d", "e"]
    assert [t["trigger"] for t in out["triggers"]] == ["Skipped lunch"]
    assert out["triggers"][0]["quote"] == QUOTE and out["triggers"][0]["outcome"] == "worse"
    assert [p["kind"] for p in out["patterns"]] == ["risk", "link"]
    assert out["patterns"][1]["label"] == "Walks help."
    assert out["worries"][1]["times"] == 1 and out["worries"][1]["came_true"] == "unclear"
    assert len(out["improve"]) == 3 and len(out["questions"]) == 3
    assert out["by_day"][0]["date"] == today_local().isoformat()


def test_weekly_extra_survives_garbage():
    out = reflect.weekly_extra("nope", [])
    assert out["summary_lines"] == [] and out["triggers"] == [] and out["by_day"] == []


def test_generate_weekly_stores_v2_fields(tmp_path):
    path = _setup(tmp_path)
    eid = add_entry(path, today_local().isoformat(), text=TEXT)
    raw = _raw(eid)
    raw["wins"] = [{"id": "w1", "text": "You noticed it.", "quote": QUOTE, "entry_id": eid}]
    conn = db.connect(path)
    now = datetime.datetime.now(datetime.timezone.utc)
    rid = reflect.generate_weekly(conn, FakeLLM([raw]), periods.week_of(today_local()), now)
    content = reflections.content(reflections.get(conn, rid))
    assert content["v"] == 2 and content["wins"][0]["quote"] == QUOTE
    assert content["triggers"][0]["trigger"] == "Skipped lunch"


def test_monthly_extra_and_summary_list(tmp_path):
    raw = {"summary": ["l1", "l2"], "patterns": [{"id": "p1", "label": "Sleep", "kind": "risk",
           "text": "Late nights.", "weeks": ["2026-W44", "nope"]}],
           "triggers": [{"trigger": "Deadline", "outcome": "helped", "count": 4, "weeks": ["2026-W44", "zz"]}],
           "worries": [{"worry": "Work", "times": 5, "came_true": "no", "weeks": ["2026-W44"]}],
           "recap": [{"week": "2026-W44", "text": "Steady."}, {"week": "2026-W99", "text": "Bad key."}],
           "improve": [{"step": "s", "why": "w"}], "questions": ["q"], "hidden_wins": ["h"],
           "focus_progress": "ok"}
    cleaned, _ = reflect._clean_monthly(raw, ["2026-W44"])
    assert cleaned["summary"] == "l1 l2" and cleaned["patterns"][0]["weeks"] == ["2026-W44"]
    assert cleaned["patterns"][0]["kind"] == "risk"
    extra = reflect.monthly_extra(raw, ["2026-W44"], [])
    assert extra["summary_lines"] == ["l1", "l2"]
    assert extra["triggers"][0]["weeks"] == ["2026-W44"] and extra["triggers"][0]["count"] == 4
    assert [r["week"] for r in extra["recap"]] == ["2026-W44"]
    assert extra["hidden_wins"] == ["h"] and extra["worries"][0]["weeks"] == ["2026-W44"]
