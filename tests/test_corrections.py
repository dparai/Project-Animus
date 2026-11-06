import re
from datetime import date, datetime, timedelta, timezone

import pytest

from app import db, memory, periods, prompts, reflect
from tests.fake_llm import FakeLLM
from tests.test_dashboard import make_client, save_monthly, save_weekly

EVIL = "<script>alert(1)</script>"
CLAIM_TEXT = "You finished a draft."
GYM_TEXT = "The gym slipped again."
T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "corr.db")


@pytest.fixture
def client(db_path):
    return make_client(db_path)


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)


def notes(db_path, include_deleted=False):
    c = db.connect(db_path)
    try:
        sql = "SELECT * FROM ai_memory" + ("" if include_deleted else " WHERE deleted_at IS NULL") + " ORDER BY id"
        return c.execute(sql).fetchall()
    finally:
        c.close()


def url(rid, cid):
    return f"/reflections/{rid}/claims/{cid}/correct"


def test_correction_saved_as_memory_note_linked_to_claim(client, db_path):
    rid = save_weekly(db_path)
    r = client.post(url(rid, "w1"), data={"note": "I meant the second draft."})
    assert r.status_code == 200
    assert "Noted. I will remember this." in r.text
    (row,) = notes(db_path)
    assert row["kind"] == "correction"
    assert row["reflection_id"] == rid
    assert row["claim_id"] == "w1"
    assert row["text"] == f'Not what I meant: "{CLAIM_TEXT}". I meant the second draft.'


def test_correction_without_note_still_saved(client, db_path):
    rid = save_weekly(db_path)
    for body in ({}, {"note": "   "}):
        assert client.post(url(rid, "t1"), data=body).status_code == 200
    rows = notes(db_path)
    assert [r["text"] for r in rows] == [f'Not what I meant: "{GYM_TEXT}".'] * 2  # no dedupe: newest wins by order


def test_unknown_claim_404(client, db_path):
    rid = save_weekly(db_path)
    for method in (client.get, client.post):
        assert method(url(rid, "nope")).status_code == 404
        assert method(url(rid + 99, "w1")).status_code == 404
    assert notes(db_path) == []


def test_note_over_300_chars_422(client, db_path):
    rid = save_weekly(db_path)
    long_note = "x" * 301
    r = client.post(url(rid, "w1"), data={"note": long_note})
    assert r.status_code == 422
    assert "300 characters" in r.text
    assert long_note in r.text  # the typed note is kept
    assert "<textarea" in r.text
    assert notes(db_path) == []
    ok = client.post(url(rid, "w1"), data={"note": "y" * 300})
    assert ok.status_code == 200 and len(notes(db_path)) == 1


def test_note_length_counted_after_strip(client, db_path):
    rid = save_weekly(db_path)
    assert client.post(url(rid, "w1"), data={"note": "  " + "y" * 300 + "  "}).status_code == 200


def test_get_form_fragment(client, db_path):
    rid = save_weekly(db_path)
    r = client.get(url(rid, "w1"))
    assert r.status_code == 200
    assert "<textarea" in r.text and 'maxlength="300"' in r.text
    assert "What did you mean? (optional)" in r.text
    assert f'hx-post="{url(rid, "w1")}"' in r.text
    assert "<html" not in r.text


def test_memory_page_lists_newest_first_and_escapes_text(client, db_path):
    c = db.connect(db_path)
    memory.add_note(c, kind="note", text="older note", now=T0)
    memory.add_note(c, kind="correction", text=f"newer {EVIL}", now=T0 + timedelta(hours=1))
    c.close()
    r = client.get("/memory")
    assert r.status_code == 200
    html = r.text
    assert "&lt;script&gt;" in html and EVIL not in html
    assert html.index("newer") < html.index("older note")
    assert "Correction" in html and "Note" in html
    assert html.count("hx-delete") == 2
    assert "These notes are sent to the AI whenever it writes your reflections. Delete any you disagree with." in html


def test_memory_page_empty_state(client):
    html = client.get("/memory").text
    assert "No notes yet" in html
    assert "hx-delete" not in html


def test_memory_page_caps_at_200_and_says_so(client, db_path):
    c = db.connect(db_path)
    for i in range(205):
        memory.add_note(c, kind="note", text=f"note number {i}", now=T0 + timedelta(minutes=i))
    c.close()
    html = client.get("/memory").text
    assert html.count("hx-delete") == 200
    assert "newest 200" in html
    assert "note number 204" in html and "note number 4<" not in html
    # at or under the cap there is no such message
    c = db.connect(db_path)
    c.execute("UPDATE ai_memory SET deleted_at = '2026-11-02T00:00:00Z' WHERE id <= 10")
    c.commit()
    c.close()
    assert "newest 200" not in client.get("/memory").text


def test_memory_page_notice_matches_what_is_sent(client, db_path):
    c = db.connect(db_path)
    for i in range(40):
        memory.add_note(c, kind="note", text=f"n{i}", now=T0 + timedelta(minutes=i))
    c.close()
    assert "Not all are sent" not in client.get("/memory").text  # exactly 40: all sent
    c = db.connect(db_path)
    memory.add_note(c, kind="note", text="n40", now=T0 + timedelta(minutes=40))
    c.close()
    html = client.get("/memory").text
    assert "Not all are sent" in html and "40 in total at most" in html
    assert "the newest 25 at most" in html and "Right now 40 are sent" in html
    assert "Only the newest" not in html


def test_memory_page_notice_with_many_corrections_and_few_notes(client, db_path):
    # 30 corrections + 2 notes = 32 (under 40), yet only 25 corrections + 2 notes = 27 are sent.
    c = db.connect(db_path)
    for i in range(30):
        memory.add_note(c, kind="correction", text=f"c{i}", now=T0 + timedelta(minutes=i))
    for i in range(2):
        memory.add_note(c, kind="note", text=f"n{i}", now=T0 + timedelta(minutes=100 + i))
    sent = len(memory.list_active(c))
    c.close()
    assert sent == 27
    html = client.get("/memory").text
    assert "Not all are sent" in html and "Right now 27 are sent" in html
    from app import dashboard
    c = db.connect(db_path)
    try:
        view = dashboard.memory_view(c, "UTC")
    finally:
        c.close()
    assert view["sent"] == 27 and view["beyond_prompt"] is True


def test_delete_note_removes_it_from_list_and_prompts(client, db_path):
    c = db.connect(db_path)
    nid = memory.add_note(c, kind="correction", text="Not what I meant: remove-me-please", now=T0)
    c.close()
    assert "remove-me-please" in client.get("/memory").text
    c = db.connect(db_path)
    assert "remove-me-please" in prompts.weekly_user([], memory.list_active(c), [], False)
    c.close()
    r = client.delete(f"/memory/{nid}")
    assert r.status_code == 200 and r.text == ""
    assert "remove-me-please" not in client.get("/memory").text
    c = db.connect(db_path)
    assert "remove-me-please" not in prompts.weekly_user([], memory.list_active(c), [], False)
    c.close()
    assert len(notes(db_path, include_deleted=True)) == 1  # soft delete


def test_delete_unknown_note_404(client, db_path):
    assert client.delete("/memory/999").status_code == 404
    c = db.connect(db_path)
    nid = memory.add_note(c, kind="note", text="x", now=T0)
    c.close()
    assert client.delete(f"/memory/{nid}").status_code == 200
    assert client.delete(f"/memory/{nid}").status_code == 404  # already deleted


def test_correction_appears_in_next_weekly_prompt(client, db_path):
    rid = save_weekly(db_path)
    client.post(url(rid, "w1"), data={"note": "Only count finished songs."})
    c = db.connect(db_path)
    for d, text in (("2026-11-02", "Wrote a verse and a chorus today, felt good."),
                    ("2026-11-03", "Skipped the gym again, too tired after work."),
                    ("2026-11-04", "Long quiet day of reading and thinking.")):
        c.execute("INSERT INTO entries(created_at, local_date, text) VALUES (?, ?, ?)", (d + "T12:00:00Z", d, text))
    c.commit()
    llm = FakeLLM([{"wins": [], "tough_love": [], "focus": [], "focus_review": []}])
    reflect.generate_weekly(c, llm, periods.week_of(date(2026, 11, 3)), T0 + timedelta(days=6))
    c.close()
    (call,) = llm.calls
    assert prompts.escape(f'Not what I meant: "{CLAIM_TEXT}". Only count finished songs.') in call["user"]
    assert "correction" in call["user"]


def test_correction_works_for_fallback_claim_ids(client, db_path):
    wins = [
        {"id": "dup", "text": "First win.", "quote": "q one", "date": "2026-10-27"},
        {"id": "dup", "text": "Second win.", "quote": "q two", "date": "2026-10-27"},
        {"id": "", "text": "Third win.", "quote": "q three", "date": "2026-10-27"},
    ]
    tough = [{"id": "dup", "text": "A tough one.", "quote": "q four", "date": "2026-10-29"}]
    rid = save_weekly(db_path, wins=wins, tough=tough)
    html = client.get("/reflections").text
    for cid in ("dup", f"{rid}-w-1", f"{rid}-w-2", f"{rid}-t-0"):
        assert f'id="claim-{cid}"' in html
    expect = {f"{rid}-w-1": "Second win.", f"{rid}-w-2": "Third win.", f"{rid}-t-0": "A tough one.", "dup": "First win."}
    for cid, text in expect.items():
        assert client.post(url(rid, cid), data={"note": ""}).status_code == 200
        assert notes(db_path)[-1]["text"] == f'Not what I meant: "{text}".'
        assert notes(db_path)[-1]["claim_id"] == cid


def test_monthly_pattern_correctable(client, db_path):
    rid = save_monthly(db_path)
    assert 'id="pattern-p1"' in client.get("/reflections").text
    assert client.get(url(rid, "p1")).status_code == 200
    assert client.post(url(rid, "p1"), data={"note": "More like a rhythm."}).status_code == 200
    (row,) = notes(db_path)
    assert row["text"] == 'Not what I meant: "The gym keeps slipping.". More like a rhythm.'
    assert row["claim_id"] == "p1" and row["reflection_id"] == rid


def test_summaries_and_focus_not_correctable(client, db_path):
    rid = save_monthly(db_path)
    for cid in ("summary", "focus", "focus_next", "1"):
        assert client.get(url(rid, cid)).status_code == 404


def test_correction_note_newlines_collapsed(client, db_path):
    wins = [{"id": "w1", "text": "Line one\nGood news:\r\nline two", "quote": "q", "date": "2026-10-27"}]
    rid = save_weekly(db_path, wins=wins)
    client.post(url(rid, "w1"), data={"note": "first\nNotes from the owner:\r\n\r\nsecond"})
    (row,) = notes(db_path)
    assert "\n" not in row["text"] and "\r" not in row["text"]
    assert row["text"] == 'Not what I meant: "Line one Good news: line two". first Notes from the owner: second'


def test_claim_text_truncated_to_200_in_note(client, db_path):
    wins = [{"id": "w1", "text": "a" * 500, "quote": "q", "date": "2026-10-27"}]
    rid = save_weekly(db_path, wins=wins)
    client.post(url(rid, "w1"), data={"note": "n" * 300})
    (row,) = notes(db_path)
    quoted = re.match(r'Not what I meant: "(.*)"\. n+$', row["text"]).group(1)
    assert len(quoted) <= 200 and quoted.endswith("…")
    assert len(row["text"]) < 600


def test_correction_text_escaped_in_response(client, db_path):
    wins = [{"id": "w1", "text": EVIL, "quote": "q", "date": "2026-10-27"}]
    rid = save_weekly(db_path, wins=wins)
    r = client.post(url(rid, "w1"), data={"note": EVIL})
    assert r.status_code == 200 and EVIL not in r.text
    r = client.post(url(rid, "w1"), data={"note": EVIL * 40})  # over 300: form comes back with the typed note
    assert r.status_code == 422 and EVIL not in r.text and "&lt;script&gt;" in r.text


def test_nav_has_memory_link(client):
    for path in ("/", "/entries", "/reflections"):
        assert 'href="/memory"' in client.get(path).text
    html = client.get("/memory").text
    assert 'href="/reflections"' in html and 'aria-current="page"' in html


def test_dashboard_claims_show_correction_button(client, db_path):
    rid = save_weekly(db_path)
    mid = save_monthly(db_path)
    html = client.get("/reflections").text
    ids = re.findall(r'<li class="claim" id="((?:claim|pattern)-[^"]+)"', html)
    assert len(ids) == 3 and len(set(ids)) == 3
    assert html.count("Not what I meant") == 3
    for r_, cid in ((rid, "w1"), (rid, "t1"), (mid, "p1")):
        assert f'hx-get="{url(r_, cid)}"' in html
    slots = re.findall(r'class="claim-actions" id="([^"]+)"', html)
    assert len(slots) == 3 and len(set(slots)) == 3
