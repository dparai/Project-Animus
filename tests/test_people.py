"""People: suggestions from entry facts, the owner's answers, the map, search and the tone note."""
import json
from datetime import date, datetime, timezone

import pytest

from app import db, entries, people, search
from app.llm import LLMError
from app.main import create_app
from tests.auth_helpers import anon_client, login_client
from tests.fake_llm import FakeLLM

TODAY = date(2026, 11, 16)


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "people.db")
    conn = db.connect(path)
    db.init_db(conn)
    conn.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


def _entry(conn, day, text, names, mood=None):
    """An entry plus the facts a model pass would have stored for it."""
    entry_id = entries.create_entry(
        conn, text=text, mood=mood, energy=None,
        now=datetime.fromisoformat(f"{day}T20:00:00").replace(tzinfo=timezone.utc), tz="UTC")
    facts = {"people": names, "events": [], "emotions": [], "worries": [], "wins": [], "decisions": []}
    conn.execute("INSERT INTO entry_facts(entry_id, data, created_at) VALUES (?, ?, '2026-11-16T00:00:00Z')",
                 (entry_id, json.dumps(facts)))
    conn.commit()
    return entry_id


def _id(conn, name):
    return conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()["id"]


def _confirmed(conn, name):
    people.sync(conn)
    people.set_status(conn, _id(conn, name), "confirmed")
    return _id(conn, name)


# --- suggestions, confirm, ignore, merge ---

def test_names_from_facts_become_suggestions_not_people(conn):
    _entry(conn, "2026-11-10", "Lunch with Mira. Then Cafe Luna.", ["Mira", "Cafe Luna"])
    view = people.overview(conn, TODAY)
    assert [s["name"] for s in view["suggested"]] == ["Cafe Luna", "Mira"]
    assert view["confirmed"] == [] and view["orbit"]["people"] == []
    assert view["suggested"][1]["snippet"] == "Lunch with Mira."


def test_old_database_gains_the_new_columns(tmp_path):
    path = str(tmp_path / "old.db")
    c = db.connect(path)
    c.execute("CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE,"
              " aliases TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)")
    c.execute("INSERT INTO people(name, created_at) VALUES ('Mira', 'x')")
    c.commit()
    db.init_db(c)
    db.init_db(c)  # running it again changes nothing
    person = people.get(c, 1)
    assert (person["status"], person["kind"], person["summary_mentions"]) == ("suggested", "person", 0)
    c.close()


def test_confirm_counts_entries_and_orders_by_how_often(conn):
    for day in ("2026-11-01", "2026-11-05", "2026-11-09"):
        _entry(conn, day, "Talked to Mira.", ["Mira"], mood="good")
    _entry(conn, "2026-09-01", "Saw Tom.", ["Tom"])
    _confirmed(conn, "Mira"), _confirmed(conn, "Tom")
    view = people.overview(conn, TODAY)
    assert [(c["name"], c["count"], c["recent"]) for c in view["confirmed"]] == [("Mira", 3, 3), ("Tom", 1, 0)]
    assert view["suggested"] == []
    spots = view["orbit"]["people"]
    assert [s["name"] for s in spots] == ["Mira", "Tom"] and all(s["ring"] == 1 for s in spots)
    assert all(0 < s["x"] < 100 and 0 < s["y"] < 100 for s in spots)


def test_map_holds_three_rings_and_counts_the_rest(conn):
    for n in range(25):
        _entry(conn, "2026-11-01", f"Met P{n:02d}.", [f"P{n:02d}"])
        _confirmed(conn, f"P{n:02d}")
    orbit = people.overview(conn, TODAY)["orbit"]
    assert len(orbit["people"]) == sum(people.RINGS) and orbit["rest"] == 25 - sum(people.RINGS)
    assert [p["ring"] for p in orbit["people"]].count(1) == people.RINGS[0] and len(orbit["rings"]) == 3
    assert len({(p["x"], p["y"]) for p in orbit["people"]}) == len(orbit["people"])  # nobody shares a spot


def test_ignored_name_stays_gone_and_is_not_offered_to_the_model(conn):
    from app import extract

    _entry(conn, "2026-11-10", "Dinner at Cafe Luna.", ["Cafe Luna"])
    people.sync(conn)
    people.set_status(conn, _id(conn, "Cafe Luna"), "ignored")
    _entry(conn, "2026-11-12", "Cafe Luna again.", ["Cafe Luna"])
    view = people.overview(conn, TODAY)
    assert view["suggested"] == [] and [g["name"] for g in view["ignored"]] == ["Cafe Luna"]
    assert extract.known_people(conn) == []


def test_merge_moves_names_and_counts(conn):
    _entry(conn, "2026-11-01", "Bhabi called.", ["Bhabi"])
    _entry(conn, "2026-11-02", "Bhabhi visited.", ["Bhabhi"])
    keep = _confirmed(conn, "Bhabhi")
    people.merge(conn, _id(conn, "Bhabi"), keep)
    view = people.overview(conn, TODAY)
    assert [(c["name"], c["count"], c["aliases"]) for c in view["confirmed"]] == [("Bhabhi", 2, ["Bhabi"])]
    assert view["suggested"] == []
    with pytest.raises(ValueError):
        people.merge(conn, keep, keep)


def test_other_names_add_absorb_and_remove(conn):
    _entry(conn, "2026-11-01", "Mum rang.", ["Mum"])
    _entry(conn, "2026-11-02", "Maa cooked.", ["Maa"])
    _entry(conn, "2026-11-03", "Tom came.", ["Tom"])
    maa, tom = _confirmed(conn, "Maa"), _confirmed(conn, "Tom")
    people.add_alias(conn, maa, "Mum")  # the waiting suggestion is absorbed
    assert people.overview(conn, TODAY)["confirmed"][0]["count"] == 2
    with pytest.raises(ValueError, match="taken"):
        people.add_alias(conn, maa, "Tom")  # two confirmed people: merge instead
    people.remove_alias(conn, maa, "Mum")
    view = people.overview(conn, TODAY)
    assert [s["name"] for s in view["suggested"]] == ["Mum"]  # entries still use it, so it asks again
    assert tom


def test_rename_keeps_the_old_name_working(conn):
    _entry(conn, "2026-11-01", "Momo called.", ["Momo"])
    momo = _confirmed(conn, "Momo")
    people.rename(conn, momo, "Monisha")
    person = people.get(conn, momo)
    assert (person["name"], person["aliases"]) == ("Monisha", ["Momo"])
    assert people.overview(conn, TODAY)["confirmed"][0]["count"] == 1
    with pytest.raises(ValueError):
        people.rename(conn, momo, "   ")


def test_detail_timeline_moods_and_companions(conn):
    _entry(conn, "2026-11-01", "Gym. Then Mira and Tom came over.", ["Mira", "Tom"], mood="good")
    _entry(conn, "2026-11-05", "Argued with Mira.", ["Mira"], mood="sad")
    _entry(conn, "2026-11-06", "Quiet day.", [], mood="good")
    mira, tom = _confirmed(conn, "Mira"), _confirmed(conn, "Tom")
    view = people.detail(conn, mira, TODAY)
    assert [t["text"] for t in view["timeline"]] == ["Argued with Mira.", "Then Mira and Tom came over."]
    assert view["timeline"][0]["mood_label"] == "Sad" and view["count"] == 2
    assert (view["mood_share"], view["mood_share_all"]) == (50, 67)
    assert [(c["id"], c["count"]) for c in view["companions"]] == [(tom, 1)]
    assert people.detail(conn, 9999, TODAY) is None


def test_own_read_is_kept_as_one_memory_note(conn):
    _entry(conn, "2026-11-01", "Mira.", ["Mira"])
    mira = _confirmed(conn, "Mira")
    people.set_read(conn, mira, "closer", TODAY)
    people.set_read(conn, mira, "apart", TODAY)
    notes = conn.execute("SELECT text FROM ai_memory WHERE deleted_at IS NULL").fetchall()
    assert len(notes) == 1 and "further apart" in notes[0]["text"] and "Mira" in notes[0]["text"]
    people.set_status(conn, mira, "ignored")  # removing the person removes their notes
    assert conn.execute("SELECT COUNT(*) FROM ai_memory WHERE deleted_at IS NULL").fetchone()[0] == 0
    with pytest.raises(ValueError):
        people.set_read(conn, mira, "enemies", TODAY)


# --- search ---

def test_question_about_a_person_finds_entries_under_every_name(conn):
    a = _entry(conn, "2026-11-01", "Momo called from a new number.", ["Momo"])
    b = _entry(conn, "2026-11-03", "Long talk with Monisha about the trip.", ["Monisha"])
    c = _entry(conn, "2026-11-04", "She was quiet at dinner.", ["Momo"])  # the name is not in the text
    _entry(conn, "2026-11-05", "Gym and groceries.", [])
    momo = _confirmed(conn, "Momo")
    people.add_alias(conn, momo, "Monisha")
    found = [r["id"] for r in search.find_entries_hybrid(conn, "what happened with momo", TODAY, 8)]
    assert set(found) == {a, b, c}


def test_unconfirmed_names_do_not_change_search(conn):
    _entry(conn, "2026-11-01", "Momo called.", ["Momo"])
    hidden = _entry(conn, "2026-11-04", "She was quiet at dinner.", ["Momo"])
    people.sync(conn)  # still only a suggestion
    found = [r["id"] for r in search.find_entries_hybrid(conn, "what happened with momo", TODAY, 8)]
    assert hidden not in found
    assert people.in_question(conn, "what happened with momo") == []


def test_names_match_whole_words_only(conn):
    _entry(conn, "2026-11-01", "Al came.", ["Al"])
    _confirmed(conn, "Al")
    assert people.in_question(conn, "I felt calm and also tired") == []
    assert [p["name"] for p in people.in_question(conn, "what did al say")] == ["Al"]


# --- tone note ---

def _three_mentions(conn):
    ids = [_entry(conn, f"2026-11-0{n}", text, ["Mira"], mood="okay") for n, text in
           ((1, "Mira and I laughed all evening."), (2, "Short call with Mira."), (3, "Mira did not answer. I felt ignored."))]
    return _confirmed(conn, "Mira"), ids


def test_tone_note_keeps_only_quotes_found_in_the_entries(conn):
    mira, ids = _three_mentions(conn)
    llm = FakeLLM([{"note": "You wrote warmly, then more guardedly.",
                    "quotes": [{"entry_id": ids[2], "quote": "Mira did not answer. I felt ignored"},
                               {"entry_id": ids[0], "quote": "we fought all night"}]}])
    assert people.refresh_due(conn, llm) == 1
    person = people.get(conn, mira)
    assert person["summary"] == "You wrote warmly, then more guardedly."
    assert person["summary_quotes"] == [{"entry_id": ids[2], "quote": "Mira did not answer. I felt ignored"}]
    assert person["summary_mentions"] == 3
    note = conn.execute("SELECT text FROM ai_memory WHERE deleted_at IS NULL").fetchone()
    assert note["text"].startswith("How I have been writing about Mira lately:")
    assert "<excerpt" in llm.calls[0]["user"] and "laughed all evening" in llm.calls[0]["user"]
    assert people.refresh_due(conn, FakeLLM()) == 0  # nothing new: no model call


def test_tone_note_without_a_checked_quote_is_not_shown(conn):
    mira, ids = _three_mentions(conn)
    llm = FakeLLM([{"note": "Something confident.", "quotes": [{"entry_id": ids[0], "quote": "never written"}]}])
    people.refresh_due(conn, llm)
    person = people.get(conn, mira)
    assert person["summary"] == "" and person["summary_quotes"] == [] and person["summary_mentions"] == 3
    assert conn.execute("SELECT COUNT(*) FROM ai_memory").fetchone()[0] == 0


def test_tone_note_survives_a_model_error_and_skips_unconfirmed(conn):
    _entry(conn, "2026-11-01", "Tom.", ["Tom"]), _entry(conn, "2026-11-02", "Tom.", ["Tom"])
    _entry(conn, "2026-11-03", "Tom.", ["Tom"])
    people.sync(conn)
    assert people.refresh_due(conn, FakeLLM()) == 0  # Tom is only a suggestion
    mira, _ = _three_mentions(conn)
    assert people.refresh_due(conn, FakeLLM([LLMError("busy", retryable=True, reason="http_429")])) == 0
    assert people.get(conn, mira)["summary_mentions"] == 0  # tried again next pass


# --- pages ---

@pytest.fixture
def client(db_path):
    return login_client(create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False),
                        follow_redirects=False)


def test_people_page_lists_the_waiting_names(client, conn):
    _entry(conn, "2026-11-10", "Lunch with Mira.", ["Mira"])
    html = client.get("/people").text
    assert "Names waiting for your answer" in html and "Lunch with Mira." in html
    assert 'class="orbit"' not in html  # nobody confirmed yet


def test_confirm_then_person_page(client, conn):
    _entry(conn, "2026-11-10", "Lunch with Mira.", ["Mira"], mood="good")
    people.sync(conn)
    mira = _id(conn, "Mira")
    r = client.post(f"/people/{mira}/confirm")
    assert r.status_code == 303 and r.headers["location"] == "/people"
    html = client.get("/people").text
    assert 'class="orbit"' in html and f'href="/people/{mira}"' in html
    page = client.get(f"/people/{mira}").text
    assert "Lunch with Mira." in page and 'href="/entries?day=2026-11-10"' in page
    assert "How are things with Mira right now?" in page
    assert client.post(f"/people/{mira}/read", data={"read": "closer"}).status_code == 303
    assert 'value="closer" aria-pressed="true"' in client.get(f"/people/{mira}").text


def test_page_forms_edit_merge_and_alias(client, conn):
    _entry(conn, "2026-11-01", "Bhabi called.", ["Bhabi"])
    _entry(conn, "2026-11-02", "Bhabhi visited.", ["Bhabhi"])
    keep = _confirmed(conn, "Bhabhi")
    other = _id(conn, "Bhabi")
    r = client.post(f"/people/{other}/merge", data={"into": str(keep)})
    assert r.status_code == 303 and r.headers["location"] == f"/people/{keep}"
    assert client.post(f"/people/{keep}/details", data={"name": "Bhabhi", "relation": "Sister-in-law", "kind": "person"}).status_code == 303
    assert client.post(f"/people/{keep}/alias", data={"alias": "B"}).status_code == 303
    person = people.get(conn, keep)
    assert person["relation"] == "Sister-in-law" and person["aliases"] == ["Bhabi", "B"]
    bad = client.post(f"/people/{keep}/details", data={"name": "", "relation": "", "kind": "person"})
    assert bad.headers["location"] == f"/people/{keep}?problem=name"
    assert "Enter a name" in client.get(bad.headers["location"]).text


def test_add_by_hand_and_bad_input(client, conn):
    r = client.post("/people", data={"name": "  Asha  "})
    assert r.status_code == 303 and people.get(conn, int(r.headers["location"].rsplit("/", 1)[1]))["status"] == "confirmed"
    assert client.post("/people", data={"name": "x" * 61}).headers["location"] == "/people?problem=name"
    assert client.get("/people/9999").status_code == 404
    assert client.post("/people/9999/confirm").status_code == 404
    assert client.post("/people/1/read", data={"read": "enemies"}).status_code == 422
    assert client.post("/people/1/merge", data={"into": "9999"}).status_code == 404


def test_names_are_escaped_on_the_pages(client, conn):
    _entry(conn, "2026-11-10", "Met <b>X</b> today.", ["<script>alert(1)</script>"])
    people.sync(conn)
    html = client.get("/people").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    person_id = _id(conn, "<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in client.get(f"/people/{person_id}").text


def test_people_needs_a_session_and_a_csrf_token(db_path, conn):
    app = create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False)
    for path in ("/people", "/people/1"):
        r = anon_client(app).get(path)
        assert r.status_code in (302, 303, 307) and "/login" in r.headers["location"]
    _entry(conn, "2026-11-10", "Mira.", ["Mira"])
    people.sync(conn)
    logged = login_client(app, follow_redirects=False)
    logged.headers.pop("X-CSRF-Token")
    assert logged.post(f"/people/{_id(conn, 'Mira')}/confirm").status_code == 403
    assert people.get(conn, _id(conn, "Mira"))["status"] == "suggested"


def test_ask_page_prefills_the_question_safely(client):
    html = client.get("/ask", params={"q": 'What about "Mira" <b>?'}).text
    assert 'value="What about &#34;Mira&#34; &lt;b&gt;?"' in html
