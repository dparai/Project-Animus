"""People connections, shared nicknames ("which dada?"), link suggestions and rename-in-entries."""
import json
from datetime import datetime, timezone

import pytest

from app import connections, db, entries, extract, people, rewrite
from app.llm import LLMError
from app.main import create_app
from tests.auth_helpers import login_client
from tests.fake_llm import FakeLLM


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "c.db")
    c = db.connect(path)
    db.init_db(c)
    c.close()
    return path


@pytest.fixture
def conn(db_path):
    c = db.connect(db_path)
    yield c
    c.close()


def _entry(conn, day, text, names):
    entry_id = entries.create_entry(
        conn, text=text, mood=None, energy=None,
        now=datetime.fromisoformat(f"{day}T20:00:00").replace(tzinfo=timezone.utc), tz="UTC")
    facts = {"people": names, "events": [], "emotions": [], "worries": [], "wins": [], "decisions": []}
    conn.execute("INSERT INTO entry_facts(entry_id, data, created_at) VALUES (?, ?, '2026-11-16T00:00:00Z')",
                 (entry_id, json.dumps(facts)))
    conn.commit()
    return entry_id


def _who(conn, name):
    people.sync(conn)
    pid = conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()["id"]
    people.set_status(conn, pid, "confirmed")
    return pid


def _cousins(conn):
    """Two confirmed cousins who are both called "dada" in the entries."""
    _entry(conn, "2026-11-01", "Dada took me out.", ["Dada"])
    munna = _who(conn, "Dada")
    people.rename(conn, munna, "Munna dada")  # the old name stays as another name
    barun = people.add(conn, "Barun dada")
    people.add_alias(conn, barun, "Dada")
    return munna, barun


# --- relations the owner sets ---

def test_owner_relations_fill_the_other_side_and_work_out_siblings(conn):
    dada, bhabi, aarna, mayra = (people.add(conn, n) for n in ("Dada", "Bhabi", "Aarna", "Mayra"))
    connections.add(conn, dada, bhabi, "spouse")
    connections.add(conn, aarna, dada, "child")
    connections.add(conn, mayra, bhabi, "child")
    page = connections.for_person(conn, dada)
    assert [g["heading"] for g in page["groups"]] == ["Parent of", "Spouse of"]
    assert {i["name"] for i in page["groups"][0]["items"]} == {"Aarna"}  # Mayra is only worked out
    assert [i["name"] for g in page["worked"] for i in g["items"] if g["heading"] == "Parent of"] == ["Mayra"]
    a = connections.for_person(conn, aarna)
    assert [i["name"] for g in a["worked"] if g["heading"] == "Sibling of" for i in g["items"]] == ["Mayra"]
    assert {i["name"] for g in a["worked"] if g["heading"] == "Child of" for i in g["items"]} == {"Bhabi"}


def test_relation_needs_two_different_people_and_a_known_kind(conn):
    a = people.add(conn, "A")
    with pytest.raises(ValueError):
        connections.add(conn, a, a, "friend")
    with pytest.raises(ValueError):
        connections.add(conn, a, people.add(conn, "B"), "boss")


def test_map_neighbours_and_merge_keeps_relations(conn):
    mum, momo, other = (people.add(conn, n) for n in ("Momo's mom", "Momo", "Momo2"))
    connections.add(conn, mum, momo, "parent")
    assert connections.neighbours(conn)[momo] == [mum]
    connections.add(conn, other, momo, "friend")
    people.merge(conn, other, mum)  # the other person becomes the same as Momo's mom
    assert connections.neighbours(conn)[mum] == [momo]


# --- shared nicknames ---

def test_shared_nickname_waits_for_an_answer_then_counts(conn):
    munna, barun = _cousins(conn)
    e1 = _entry(conn, "2026-11-02", "Dada and Bhabi came.", ["Dada", "Bhabi"])
    assert extract.ambiguous_names(extract.known_people(conn)) == {"dada"}
    assert people.mentions(conn).get(munna, []) == [] and people.mentions(conn).get(barun, []) == []
    todo = people.review(conn)
    assert {t["entry_id"] for t in todo} >= {e1} and [c["name"] for c in todo[0]["candidates"]] == ["Munna dada", "Barun dada"]
    people.choose(conn, e1, "Dada", barun)
    assert people.mentions(conn)[barun] == [e1]
    assert e1 not in {t["entry_id"] for t in people.review(conn)}


def test_a_related_person_in_the_entry_marks_the_likely_dada(conn):
    munna, barun = _cousins(conn)
    bhabi = people.add(conn, "Bhabi")
    connections.add(conn, bhabi, barun, "spouse")
    e = _entry(conn, "2026-11-03", "Dada and Bhabi came.", ["Dada", "Bhabi"])
    item = next(t for t in people.review(conn) if t["entry_id"] == e)
    assert [c["id"] for c in item["candidates"] if c["likely"]] == [barun]


def test_extractor_does_not_map_or_create_a_shared_nickname(conn):
    _cousins(conn)
    row = conn.execute("SELECT * FROM entries LIMIT 1").fetchone()
    known = extract.known_people(conn)
    assert extract._canonical(known, "dada") == "dada"
    assert "also: Dada" not in extract.build_prompt(row, known)
    llm = FakeLLM([{"people": ["dada", "Bhabi"], "events": [], "emotions": [], "worries": [], "wins": [], "decisions": []}])
    assert extract.extract_entry(conn, llm, row["id"])
    names = {r["name"].casefold() for r in conn.execute("SELECT name FROM people")}
    assert "dada" not in names and "bhabi" in names


# --- suggestions from entries ---

def test_scan_suggests_links_that_wait_for_the_owner(conn):
    _entry(conn, "2026-11-04", "Momo's mom gave Momo a hug.", ["Momo"])
    momo = _who(conn, "Momo")
    llm = FakeLLM([{"links": [{"a": "Momo's mom", "relation": "parent", "b": "Momo", "quote": "Momo's mom"},
                              {"a": "Momo", "relation": "boss", "b": "Momo", "quote": ""}]}])
    assert connections.scan_due(conn, llm) == 1
    wait = connections.waiting(conn)
    assert len(wait) == 1 and wait[0]["b_id"] == momo and wait[0]["quote"] == "Momo's mom"
    assert connections.for_person(conn, momo)["groups"] == []  # a suggestion is not a relation yet
    assert connections.pending_count(conn) == 0  # not asked about the same entry twice
    connections.confirm(conn, wait[0]["link_id"])
    assert connections.for_person(conn, momo)["groups"][0]["heading"] == "Child of"


def test_dismissed_suggestion_is_not_made_again(conn):
    e = _entry(conn, "2026-11-04", "Momo's mom hugged Momo.", ["Momo"])
    _who(conn, "Momo")
    reply = {"links": [{"a": "Momo's mom", "relation": "parent", "b": "Momo", "quote": ""}]}
    connections.scan_due(conn, FakeLLM([reply]))
    connections.remove(conn, connections.waiting(conn)[0]["link_id"])
    conn.execute("DELETE FROM link_scan")
    connections.scan_due(conn, FakeLLM([reply]))
    assert connections.waiting(conn) == [] and e


def test_scan_stops_on_model_failure_and_retries_later(conn):
    _entry(conn, "2026-11-04", "Momo's mom hugged Momo.", ["Momo"])
    assert connections.scan_due(conn, FakeLLM([LLMError("timeout", retryable=True)])) == 0
    assert connections.pending_count(conn) == 1


def test_fix_replaces_a_wrong_suggestion(conn):
    _entry(conn, "2026-11-04", "Asha and Momo.", ["Asha", "Momo"])
    asha, momo = _who(conn, "Asha"), _who(conn, "Momo")
    connections.scan_due(conn, FakeLLM([{"links": [{"a": "Asha", "relation": "parent", "b": "Momo", "quote": ""}]}]))
    link = connections.waiting(conn)[0]
    connections.fix(conn, link["link_id"], asha, momo, "child")
    assert connections.waiting(conn) == []
    assert connections.for_person(conn, asha)["groups"][0]["heading"] == "Child of"


# --- rename inside entries ---

def test_rename_preview_apply_and_undo(conn):
    munna, barun = _cousins(conn)
    e1 = conn.execute("SELECT id FROM entries ORDER BY id LIMIT 1").fetchone()["id"]
    e2 = _entry(conn, "2026-11-05", "Dada rang. Munna dada also texted.", ["Dada"])
    people.choose(conn, e1, "Dada", munna)
    people.choose(conn, e2, "Dada", barun)  # this one is Barun's, never Munna's
    items = rewrite.candidates(conn, munna, "Dada")
    assert [i["entry_id"] for i in items] == [e1] and "Munna dada took me out." in items[0]["after"]
    batch, n = rewrite.apply(conn, munna, "Dada", [e1, e2])  # e2 is not Munna's: ignored
    assert n == 1
    text = lambda i: conn.execute("SELECT text FROM entries WHERE id = ?", (i,)).fetchone()["text"]
    assert text(e1) == "Munna dada took me out." and text(e2) == "Dada rang. Munna dada also texted."
    assert people.mentions(conn)[munna] == [e1]  # facts were put back with the new name
    assert rewrite.undo(conn, batch) == 1 and text(e1) == "Dada took me out."


def test_rewrite_leaves_text_already_using_the_new_name(conn):
    pid = people.add(conn, "Sam")
    e = _entry(conn, "2026-11-06", "Sam and sammy met. Sam again.", ["Sam"])
    people.rename(conn, pid, "Samuel")
    people.add_alias(conn, pid, "Sammy")
    _, n = rewrite.apply(conn, pid, "Sam", [e])
    assert n == 1
    assert conn.execute("SELECT text FROM entries WHERE id = ?", (e,)).fetchone()["text"] == "Samuel and sammy met. Samuel again."


# --- pages ---

@pytest.fixture
def client(db_path):
    return login_client(create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False),
                        follow_redirects=False)


def test_pages_review_links_and_rename_flow(client, conn):
    munna, barun = _cousins(conn)
    e = _entry(conn, "2026-11-07", "Dada called.", ["Dada"])
    _entry(conn, "2026-11-08", "Munna dada is here.", ["Munna dada"])
    assert "Which person?" in client.get("/people/review").text
    assert 'href="/people/review"' in client.get("/people").text
    assert client.post("/people/review/choose", data={"entry_id": e, "name": "Dada", "person_id": barun}).status_code == 303
    assert client.post(f"/people/{munna}/link", data={"choice": "cousin", "other": barun}).status_code == 303
    assert "Family and connections" in client.get(f"/people/{munna}").text
    assert "Cousin of" in client.get(f"/people/{munna}").text
    assert client.get("/people/links").status_code == 200
    r = client.post(f"/people/{munna}/details", data={"name": "Munna da", "relation": "", "kind": "person"})
    assert r.status_code == 303 and "/rewrite?old=" in r.headers["location"]
    assert "Change" in client.get(r.headers["location"]).text
