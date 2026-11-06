from datetime import datetime, timezone

import pytest

from app import db, periods, reflections
from tests.test_dashboard import add_entry, make_client, today_local

TEXT = "Long day. I skipped lunch again and felt drained. Then I scrolled for an hour. Not proud of it."
QUOTE = "skipped lunch again and felt drained"


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "ui.db")
    c = db.connect(path)
    db.init_db(c)
    c.close()
    return path


def _save(path, kind, content, period):
    c = db.connect(path)
    rid = reflections.save(
        c, kind=kind, period_key=period.key, period_start=period.start.isoformat(),
        period_end=period.end.isoformat(), model="m", content=content, now=datetime.now(timezone.utc),
    )
    c.close()
    return rid


def _v2_weekly(eid):
    return {"v": 2, "wins": [], "tough_love": [], "focus": [{"id": 1, "text": "Eat lunch"}], "focus_review": [],
            "short": False, "dropped": [], "unverified": False, "entry_count": 1,
            "summary_lines": ["One", "Two"], "overall_feel": "Tired week.",
            "patterns": [{"label": "Sleep", "kind": "risk", "text": "Late nights."}],
            "triggers": [{"trigger": "Skipped lunch", "felt": "Drained", "did": "Scrolled", "outcome": "worse",
                          "quote": QUOTE, "entry_id": eid, "date": today_local().isoformat()}],
            "worries": [{"worry": "Falling behind", "times": 3, "came_true": "no", "note": "Finished early."}],
            "improve": [{"step": "Fix a bedtime", "why": "Early nights help."}],
            "questions": ["What helped Thursday?"], "takeaway": "Meals matter.",
            "by_day": [{"date": today_local().isoformat(), "mood": "rough", "energy": 4}]}


@pytest.fixture
def seeded(db_path):
    eid = add_entry(db_path, today_local().isoformat(), text=TEXT)
    period = periods.week_of(today_local())
    rid = _save(db_path, "weekly", _v2_weekly(eid), period)
    return eid, rid


def test_view_renders_v2_sections_and_ribbon(db_path, seeded):
    eid, rid = seeded
    r = make_client(db_path).get(f"/reflections/{rid}/view")
    assert r.status_code == 200
    html = r.text
    for text in ("Summary", "How you felt", "Patterns found", "Triggers and responses", "Skipped lunch",
                 "Recurring worries", "Did not come true", "How you can improve", "Questions to think about",
                 "not medical therapy", "Entries from this week", "Long day. I skipped lunch again"):
        assert text in html
    assert f"/reflections/{rid}/entries/{eid}/note" in html


def test_ribbon_preview_is_first_sentences_only(db_path, seeded):
    eid, rid = seeded
    html = make_client(db_path).get(f"/reflections/{rid}/view").text
    assert "Then I scrolled for an hour." in html and "Not proud of it." not in html.split('class="ent-text"')[1].split("</span>")[0]


def test_old_format_report_still_renders(db_path):
    eid = add_entry(db_path, today_local().isoformat(), text=TEXT)
    old = {"wins": [{"id": "w1", "text": "Good.", "quote": QUOTE, "entry_id": eid, "date": today_local().isoformat()}],
           "tough_love": [], "focus": [], "focus_review": [], "short": False, "dropped": [], "unverified": False}
    rid = _save(db_path, "weekly", old, periods.week_of(today_local()))
    html = make_client(db_path).get(f"/reflections/{rid}/view").text
    assert "Wins" in html and "Entries from this week" in html


def test_view_unknown_is_404(db_path):
    assert make_client(db_path).get("/reflections/999/view").status_code == 404


def test_note_form_and_save_goes_to_memory(db_path, seeded):
    eid, rid = seeded
    client = make_client(db_path)
    form = client.get(f"/reflections/{rid}/entries/{eid}/note")
    assert form.status_code == 200 and "Save to memory" in form.text
    r = client.post(f"/reflections/{rid}/entries/{eid}/note", data={"note": "I was harder on myself than needed."})
    assert r.status_code == 200 and "Saved to memory" in r.text
    c = db.connect(db_path)
    rows = c.execute("SELECT kind, text, reflection_id FROM ai_memory").fetchall()
    c.close()
    assert len(rows) == 1 and rows[0]["kind"] == "note" and rows[0]["reflection_id"] == rid
    assert rows[0]["text"].startswith("My thoughts on my entry from ") and "harder on myself" in rows[0]["text"]


def test_note_blank_and_too_long_rejected(db_path, seeded):
    eid, rid = seeded
    client = make_client(db_path)
    assert client.post(f"/reflections/{rid}/entries/{eid}/note", data={"note": "  "}).status_code == 422
    assert client.post(f"/reflections/{rid}/entries/{eid}/note", data={"note": "x" * 1001}).status_code == 422
    c = db.connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM ai_memory").fetchone()[0] == 0
    c.close()


def test_note_unknown_entry_or_reflection_404(db_path, seeded):
    eid, rid = seeded
    client = make_client(db_path)
    assert client.get(f"/reflections/{rid}/entries/9999/note").status_code == 404
    assert client.get(f"/reflections/9999/entries/{eid}/note").status_code == 404


def test_page_lists_each_report_by_date_range_latest_open(db_path, seeded):
    eid, rid = seeded
    older = periods.week_of(today_local() - __import__("datetime").timedelta(days=7))
    _save(db_path, "weekly", _v2_weekly(eid), older)
    html = make_client(db_path).get("/reflections").text
    assert html.count('class="acc-row"') >= 2
    assert 'aria-expanded="true"' in html and html.count(' to ') >= 2
    assert "Triggers and responses" in html  # latest report is rendered in place
