import json
import re
import threading
from html import unescape
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db, focus, periods, reflections
from app.main import create_app
from tests.auth_helpers import login_client
from tests.fake_llm import FakeLLM

NOW = datetime(2026, 11, 1, 5, 0, 0, tzinfo=timezone.utc)
QUOTE = "I skipped the gym again because I was tired after work."
EVIL = "<script>alert(1)</script>"
ZONE = ZoneInfo("America/Los_Angeles")


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "dash.db")


def make_client(db_path, llm=None):
    return login_client(create_app(db_path, allowed_hosts=["testserver"], llm=llm, start_scheduler=False))


@pytest.fixture
def client(db_path):
    return make_client(db_path)


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)


def today_local():
    return datetime.now(timezone.utc).astimezone(ZONE).date()


def add_entry(db_path, local_date, text=QUOTE, mood="good", energy=6):
    c = db.connect(db_path)
    try:
        cur = c.execute(
            "INSERT INTO entries(created_at, local_date, text, mood, energy_score)"
            " VALUES (?, ?, ?, ?, ?)",
            (f"{local_date}T18:00:00Z", local_date, text, mood, energy),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def save_weekly(db_path, wins=None, tough=None, focus_items=None, day=date(2026, 10, 28), **extra):
    p = periods.week_of(day)
    content = {
        "wins": wins if wins is not None else [
            {"id": "w1", "text": "You finished a draft.", "quote": "Finished the first draft of the song.",
             "entry_id": 1, "date": "2026-10-27"}],
        "tough_love": tough if tough is not None else [
            {"id": "t1", "text": "The gym slipped again.", "quote": QUOTE, "entry_id": 2, "date": "2026-10-29"}],
        "focus": focus_items if focus_items is not None else [{"id": 1, "text": "Go to the gym twice."}],
        "focus_review": [], "short": False, "dropped": [], "unverified": False, **extra,
    }
    c = db.connect(db_path)
    try:
        return reflections.save(c, kind="weekly", period_key=p.key, period_start=p.start.isoformat(),
                                period_end=p.end.isoformat(), model="m", content=content, now=NOW)
    finally:
        c.close()


def save_monthly(db_path, patterns=None):
    p = periods._month_of(date(2026, 10, 15))
    content = {
        "summary": "A month of drafts and a gym habit that keeps slipping.",
        "patterns": patterns if patterns is not None else [
            {"id": "p1", "text": "The gym keeps slipping.", "weeks": ["2026-W41", "2026-W43"]}],
        "focus_next": [{"text": "Fix a gym time."}], "weeks_used": ["2026-W41", "2026-W43"],
        "short": False, "dropped": [], "unverified": False,
    }
    c = db.connect(db_path)
    try:
        return reflections.save(c, kind="monthly", period_key=p.key, period_start=p.start.isoformat(),
                                period_end=p.end.isoformat(), model="m", content=content, now=NOW)
    finally:
        c.close()


def add_job(db_path, kind="weekly", key="2026-W44", status="pending", attempts=0,
            last_error=None, next_attempt_at=None):
    c = db.connect(db_path)
    try:
        c.execute(
            "INSERT INTO reflection_jobs(kind, period_key, status, attempts, next_attempt_at, last_error,"
            " created_at, queued_at) VALUES (?, ?, ?, ?, ?, ?, '2026-11-01T05:00:00Z', '2026-11-01T05:00:00Z')",
            (kind, key, status, attempts, next_attempt_at, last_error),
        )
        c.commit()
    finally:
        c.close()


def set_job(db_path, **cols):
    c = db.connect(db_path)
    try:
        c.execute("UPDATE reflection_jobs SET " + ", ".join(f"{k} = ?" for k in cols), tuple(cols.values()))
        c.commit()
    finally:
        c.close()


def counts(db_path):
    c = db.connect(db_path)
    try:
        return (c.execute("SELECT COUNT(*) FROM reflection_jobs").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM reflections").fetchone()[0])
    finally:
        c.close()


def test_dashboard_empty_state(client):
    r = client.get("/reflections")
    assert r.status_code == 200
    html = r.text
    assert "No reflection yet" in html
    assert html.count("Generate now") >= 2  # one button per panel (weekly and monthly)
    assert "<h1" in html and "Reflections" in html
    assert "Focus areas" in html and "No focus areas yet" in html
    assert 'hx-post="/reflections/generate?kind=weekly"' in html
    assert 'hx-post="/reflections/generate?kind=monthly"' in html


def test_dashboard_shows_latest_reflection_with_quotes_and_dates(client, db_path):
    save_weekly(db_path)
    save_monthly(db_path)
    c = db.connect(db_path)
    focus.add(c, "Go to the gym twice.", None, NOW)
    c.close()
    html = client.get("/reflections").text
    for text in ("Wins", "Tough love", "Current focus", "You finished a draft.", "The gym slipped again.",
                 QUOTE, "Finished the first draft of the song.", "Tue 27 Oct", "Thu 29 Oct",
                 "Last weekly reflection: week of 26 Oct", "Last monthly reflection: October 2026",
                 "A month of drafts", "The gym keeps slipping.", "5 Oct", "19 Oct", "Fix a gym time.",
                 "Quotes are checked against your entries. The wording around them is the AI's own."):
        assert text in html, text
    # each claim carries a stable hook for the correction control (Task 8)
    assert 'data-claim-id="w1"' in html and 'id="claim-w1"' in html and 'data-claim-id="t1"' in html


def test_model_text_escaped(client, db_path):
    save_weekly(db_path, wins=[{"id": "w1", "text": EVIL, "quote": EVIL, "entry_id": 1, "date": "2026-10-27"}],
                focus_items=[{"id": 1, "text": EVIL}])
    save_monthly(db_path, patterns=[{"id": "p1", "text": EVIL, "weeks": ["2026-W41"]}])
    c = db.connect(db_path)
    focus.add(c, EVIL, None, NOW)
    c.close()
    page, fragment = client.get("/reflections").text, client.get("/focus").text
    for html in (page, fragment):
        assert EVIL not in html
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html


def test_focus_tick_done_and_reopen(client, db_path):
    c = db.connect(db_path)
    fid = focus.add(c, "Go to the gym twice.", None, NOW)
    c.close()
    r = client.post(f"/focus/{fid}/status", data={"status": "done"})
    assert r.status_code == 200 and 'id="focus"' in r.text and 'aria-checked="true"' in r.text
    c = db.connect(db_path)
    row = c.execute("SELECT status, completed_at FROM focus_areas WHERE id = ?", (fid,)).fetchone()
    assert row["status"] == "done" and row["completed_at"]
    c.close()
    r = client.post(f"/focus/{fid}/status", data={"status": "active"})
    assert r.status_code == 200 and 'aria-checked="false"' in r.text
    c = db.connect(db_path)
    row = c.execute("SELECT status, completed_at FROM focus_areas WHERE id = ?", (fid,)).fetchone()
    assert row["status"] == "active" and row["completed_at"] is None
    c.close()
    assert "Go to the gym twice." in client.get("/focus").text


def test_focus_unknown_id_404_bad_status_422(client, db_path):
    c = db.connect(db_path)
    fid = focus.add(c, "Go to the gym twice.", None, NOW)
    c.close()
    assert client.post("/focus/999/status", data={"status": "done"}).status_code == 404
    assert client.post(f"/focus/{fid}/status", data={"status": "failing"}).status_code == 422
    assert client.post(f"/focus/{fid}/status", data={"status": "nonsense"}).status_code == 422
    assert client.post(f"/focus/{fid}/status", data={}).status_code == 422


def test_failing_focus_badge_shown(client, db_path):
    c = db.connect(db_path)
    a = focus.add(c, "Go to the gym twice.", None, NOW)
    focus.add(c, "Write every day.", None, NOW)
    focus.set_status(c, a, "failing", NOW)
    c.close()
    html = client.get("/focus").text
    assert html.count("Keeps coming back") == 1
    assert "Write every day." in html


def test_generate_now_without_key_shows_no_key_message_and_creates_no_reflection(client, db_path):
    add_entry(db_path, today_local().isoformat())
    r = client.post("/reflections/generate?kind=weekly")
    assert r.status_code == 200
    assert "No API key configured." in r.text
    assert "NVIDIA_API_KEY" in r.text and ".env" in r.text
    assert "hx-trigger" not in r.text
    assert counts(db_path) == (0, 0)  # no job row, no reflection


def test_generate_now_with_fake_llm_creates_reflection(db_path):
    llm = FakeLLM()
    client = make_client(db_path, llm)  # creates the schema
    eid = add_entry(db_path, today_local().isoformat())
    llm.queue({
        "wins": [], "tough_love": [{"id": "t1", "text": "The gym slipped.", "quote": QUOTE, "entry_id": eid}],
        "focus": [{"text": "Go to the gym twice."}], "focus_review": [],
    })
    r = client.post("/reflections/generate?kind=weekly")
    assert r.status_code == 200 and "Generating..." in r.text and "every 3s" in r.text
    c = db.connect(db_path)
    row = reflections.latest(c, "weekly")
    assert row is not None
    assert reflections.content(row)["tough_love"][0]["text"] == "The gym slipped."
    assert c.execute("SELECT status FROM reflection_jobs").fetchone()["status"] == "done"
    c.close()
    html = client.get("/reflections").text
    assert "The gym slipped." in html and "Go to the gym twice." in html


def test_generate_unknown_kind_400(client, db_path):
    assert client.post("/reflections/generate?kind=yearly").status_code == 400
    assert client.post("/reflections/generate").status_code == 400
    assert client.get("/reflections/status?kind=yearly").status_code == 400
    assert counts(db_path) == (0, 0)


def test_pending_and_failed_states_shown(client, db_path):
    def state():
        return client.get("/reflections/status?kind=weekly").text

    assert "No reflection yet" in state()
    add_job(db_path, status="pending", attempts=0, last_error="no API key configured",
            next_attempt_at="2099-01-01T00:00:00Z")
    assert "Waiting for an API key" in state()
    set_job(db_path, last_error="rate_limit", attempts=2, next_attempt_at="2099-01-01T00:00:00Z")
    assert "Pending, will retry" in state()
    set_job(db_path, status="running", last_error=None)
    assert "Generating..." in state()
    set_job(db_path, status="failed", last_error="truncated", attempts=1)
    html = state()
    assert "Failed:" in html and "cut off" in html and "truncated" not in html
    set_job(db_path, status="skipped", last_error=None)
    assert "Nothing to reflect on" in state()
    set_job(db_path, status="done")
    assert "No reflection yet" in state()
    # the page shows the same state line
    set_job(db_path, status="failed", last_error="ValueError")
    assert "Failed:" in client.get("/reflections").text


def test_no_raw_error_text_in_failed_state(client, db_path):
    add_job(db_path, status="failed", last_error="ValueError")
    for html in (client.get("/reflections/status?kind=weekly").text, client.get("/reflections").text):
        assert "Failed:" in html and "something went wrong" in html
        assert "ValueError" not in html
    set_job(db_path, last_error="sk-" + "nvapi" + "-SECRET-123 leaked")
    for html in (client.get("/reflections/status?kind=weekly").text, client.get("/reflections").text):
        assert "SECRET" not in html and "sk-nvapi" not in html


def test_status_fragment_polls_until_done(client, db_path):
    add_job(db_path, status="pending")
    html = client.get("/reflections/status?kind=weekly").text
    assert "hx-trigger" in html and "every 3s" in html and "/reflections/status?kind=weekly" in html
    set_job(db_path, status="running")
    assert "every 3s" in client.get("/reflections/status?kind=weekly").text
    save_weekly(db_path)
    set_job(db_path, status="done")
    r = client.get("/reflections/status?kind=weekly", headers={"HX-Request": "true"})
    assert "hx-trigger" not in r.text and "Last weekly reflection" in r.text
    assert r.headers.get("HX-Refresh") == "true"  # the panel and the state line reload once it is done
    assert client.get("/reflections/status?kind=weekly").headers.get("HX-Refresh") is None


def test_nav_has_reflections_link(client):
    for path in ("/", "/entries", "/reflections"):
        html = client.get(path).text
        assert 'href="/reflections"' in html, path
    page = client.get("/reflections").text
    assert re.search(r'href="/reflections"[^>]*aria-current="page"', page)
    assert 'href="/"' in page and 'href="/entries"' in page


def test_chart_has_text_alternative_and_14_bars_or_gaps(client, db_path):
    add_entry(db_path, today_local().isoformat(), energy=7)
    html = client.get("/reflections").text
    assert len(re.findall(r'data-day="\d{4}-\d{2}-\d{2}"', html)) == 14
    assert html.count('class="bar"') == 1
    assert "<desc" in html and "<title" in html and 'role="img"' in html
    assert re.search(r"<table[^>]*>.*?</table>", html, re.S)
    assert "Energy from imported entries is approximate" in html
    assert "gradient" not in html.lower().split("<body")[1]


def test_stats_tiles_empty_and_filled(client, db_path):
    html = client.get("/reflections").text
    assert "Entries this week" in html and "Average energy" in html and "Mood this week" in html
    today = today_local().isoformat()
    add_entry(db_path, today, mood="good", energy=7)
    add_entry(db_path, today, mood="low", energy=4)
    assert "5.5" in client.get("/reflections").text


def weekly_response(eid):
    return {"wins": [], "tough_love": [{"id": "t1", "text": "The gym slipped.", "quote": QUOTE, "entry_id": eid}],
            "focus": [], "focus_review": []}


def versions(db_path):
    c = db.connect(db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM reflections WHERE kind = 'weekly'").fetchone()[0]
    finally:
        c.close()


class BlockingLLM(FakeLLM):
    def __init__(self, responses):
        super().__init__(responses)
        self.started = threading.Event()
        self.release = threading.Event()

    def complete_json(self, system, user, *, max_tokens=1500):
        self.started.set()
        assert self.release.wait(10)
        return super().complete_json(system, user, max_tokens=max_tokens)


def test_double_generate_click_calls_llm_once(db_path):
    llm = BlockingLLM([])
    first = make_client(db_path, llm)
    eid = add_entry(db_path, today_local().isoformat())
    llm.queue(weekly_response(eid))
    second = login_client(first.app)
    t1 = threading.Thread(target=lambda: first.post("/reflections/generate?kind=weekly"))
    t1.start()
    assert llm.started.wait(10)  # the first job is running inside the model call
    t2 = threading.Thread(target=lambda: second.post("/reflections/generate?kind=weekly"))
    t2.start()
    time.sleep(0.5)  # the second click has queued its own run, which waits for the lock
    llm.release.set()
    t1.join(15)
    t2.join(15)
    assert len(llm.calls) == 1
    assert versions(db_path) == 1


def test_second_click_after_completion_regenerates_deliberately(db_path):
    llm = FakeLLM()
    client = make_client(db_path, llm)
    eid = add_entry(db_path, today_local().isoformat())
    llm.queue(weekly_response(eid))
    llm.queue(weekly_response(eid))
    client.post("/reflections/generate?kind=weekly")
    client.post("/reflections/generate?kind=weekly")
    assert len(llm.calls) == 2 and versions(db_path) == 2  # a click after completion is a new version


def test_entry_count_is_stored_at_generation_time(db_path):
    llm = FakeLLM()
    client = make_client(db_path, llm)
    eid = add_entry(db_path, today_local().isoformat())
    llm.queue(weekly_response(eid))
    client.post("/reflections/generate?kind=weekly")
    assert "Based on 1 entry" in client.get("/reflections").text
    later = add_entry(db_path, today_local().isoformat(), text="Another fake entry that came later.")
    add_entry(db_path, today_local().isoformat(), text="And one more fake entry.")
    assert "Based on 1 entry" in client.get("/reflections").text
    c = db.connect(db_path)
    c.execute("DELETE FROM entries WHERE id = ?", (later,))
    c.commit()
    c.close()
    assert "Based on 1 entry" in client.get("/reflections").text


def test_missing_entry_count_hides_the_count_not_crashes(client, db_path):
    save_weekly(db_path)  # fixture content has no entry_count (an older row)
    r = client.get("/reflections")
    assert r.status_code == 200
    assert "Based on" not in r.text
    assert "26 Oct to 1 Nov" in r.text


def test_focus_button_refocus_hook_present(client, db_path):
    c = db.connect(db_path)
    focus.add(c, "Go to the gym twice.", None, NOW)
    c.close()
    html = client.get("/reflections").text
    assert 'data-focus-id="1"' in html and "htmx:afterSettle" in html and "data-focus-id" in html.split("<script nonce=")[-2]


def test_status_fragment_stops_polling_after_cap(client, db_path):
    add_job(db_path, status="running")
    live = client.get("/reflections/status?kind=weekly&n=59")
    assert "every 3s" in live.text and "n=60" in live.text
    capped = client.get("/reflections/status?kind=weekly&n=60", headers={"HX-Request": "true"})
    assert "Still working. Reload the page to check." in capped.text
    assert "hx-trigger" not in capped.text
    assert capped.headers.get("HX-Refresh") is None  # no reload loop


def test_missing_claim_ids_get_unique_stable_fallbacks(client, db_path):
    rid = save_weekly(db_path, wins=[
        {"id": "", "text": "One.", "quote": "q1", "entry_id": 1, "date": "2026-10-27"},
        {"text": "Two.", "quote": "q2", "entry_id": 1, "date": "2026-10-27"},
        {"id": "w9", "text": "Three.", "quote": "q3", "entry_id": 1, "date": "2026-10-27"},
        {"id": "w9", "text": "Dup.", "quote": "q4", "entry_id": 1, "date": "2026-10-27"}],
        tough=[{"id": "", "text": "Four.", "quote": "q5", "entry_id": 1, "date": "2026-10-27"}])
    html = client.get("/reflections").text
    ids = re.findall(r'<li class="claim" id="(claim-[^"]*)"', html)
    assert len(ids) == 5 and len(set(ids)) == 5
    assert "claim-" not in [i for i in ids if i.endswith("-")]
    assert f"claim-{rid}-w-0" in ids and "claim-w9" in ids and f"claim-{rid}-t-0" in ids
    assert client.get("/reflections").text.count(f'id="claim-{rid}-w-0"') == 1  # stable across loads


@pytest.mark.parametrize("token,phrase", [
    ("http_401", "the API key was rejected"), ("http_403", "the API key was rejected"),
    ("http_404", "the model name was not found"), ("truncated", "the model's answer was cut off"),
    ("no_json", "the model's answer could not be read"), ("bad_shape", "the model's answer could not be read"),
    ("timeout", "the AI service did not answer"), ("unreachable", "the AI service did not answer"),
    ("http_429", "the AI service did not answer"), ("http_5xx", "the AI service did not answer"),
    ("http_408", "the AI service did not answer"), ("http_4xx", "something went wrong, try again"),
    ("error", "something went wrong, try again"), ("WhateverError", "something went wrong, try again"),
])
def test_failed_state_phrase_per_reason_token(client, db_path, token, phrase):
    add_job(db_path, status="failed", last_error=token, attempts=1)
    for html in (client.get("/reflections/status?kind=weekly").text, client.get("/reflections").text):
        html = unescape(html)
        assert "Failed:" in html and phrase in html
        if "_" in token or token.endswith("Error"):
            assert token not in html
