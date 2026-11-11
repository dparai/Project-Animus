"""Hybrid search, entry facts, notes that replace older notes, pattern evidence."""
from datetime import date, datetime, timedelta, timezone

from app import chat_memory, embeddings, entries, extract, memory, prompts, reflect, search
from app.llm import LLMError
from tests.fake_llm import FakeLLM
from tests.test_embeddings import FakeEmbedder

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 20)


def add(conn, text, days):
    return entries.create_entry(conn, text=text, mood=None, energy=None, now=BASE + timedelta(days=days), tz="UTC")


def test_hybrid_finds_meaning_match_keyword_search_misses(conn):
    old = add(conn, "everything feels like too much", 0)
    add(conn, "planted things in the garden", 1)
    emb = FakeEmbedder()
    embeddings.fill_missing(conn, emb)
    q = "when was I stressed"
    assert old not in [r["id"] for r in search.find_entries(conn, q, TODAY)]
    qvec = embeddings.embed_query(emb, q)
    assert search.find_entries_hybrid(conn, q, TODAY, 8, qvec)[0]["id"] == old


def test_hybrid_without_vector_is_keyword_search(conn):
    add(conn, "long walk by the river", 0)
    q = "river walk"
    assert [r["id"] for r in search.find_entries_hybrid(conn, q, TODAY, 8, None)] == \
        [r["id"] for r in search.find_entries(conn, q, TODAY, 8)]


def test_hybrid_adds_confirmed_linked_entry(conn):
    a = add(conn, "so stressed about the move", 0)
    b = add(conn, "quiet day with tea", 3)
    for i in range(4, 14):
        add(conn, f"garden notes {i}", i)
    conn.execute(
        "INSERT INTO entry_links(from_entry_id, to_entry_id, link_type, source, status, created_at)"
        " VALUES (?, ?, 'follow_up', 'owner', 'confirmed', '2026-10-05T00:00:00Z')", (b, a))
    conn.commit()
    emb = FakeEmbedder()
    embeddings.fill_missing(conn, emb)
    qvec = embeddings.embed_query(emb, "stressed")
    got = [r["id"] for r in search.find_entries_hybrid(conn, "stressed", TODAY, 2, qvec)]
    assert got[0] == a and b in got


def test_embed_query_never_raises():
    class Broken:
        def embed(self, texts, task="document"):
            raise RuntimeError("x")
    assert embeddings.embed_query(Broken(), "q") is None
    assert embeddings.embed_query(None, "q") is None


def test_long_entry_gets_part_vectors_and_edit_clears_them(conn):
    long_text = "\n".join(["calm morning by the window with coffee and a book. " * 8] * 5 + ["so stressed tonight"])
    a = add(conn, long_text, 0)
    emb = FakeEmbedder()
    embeddings.fill_missing(conn, emb)
    assert conn.execute("SELECT COUNT(*) FROM vectors WHERE kind = 'chunk' AND ref_id = ?", (a,)).fetchone()[0] > 1
    entries.update_entry(conn, a, text="short now", mood=None, energy=None)
    assert conn.execute("SELECT COUNT(*) FROM vectors WHERE kind = 'chunk'").fetchone()[0] == 0


def test_split_parts_short_text_has_none():
    assert embeddings.split_parts("short") == []


def test_entry_text_carries_day_and_mood():
    text = embeddings.entry_text({"local_date": "2026-10-05", "mood": "low", "energy_score": 3, "text": "hello"})
    assert "Monday" in text and "mood low" in text and "energy 3/10" in text and text.endswith("hello")


def test_relevant_notes_are_kept_when_too_many(conn):
    for i in range(6):
        memory.add_note(conn, kind="note", text=f"likes the garden {i}", now=BASE + timedelta(minutes=i))
    old = memory.add_note(conn, kind="note", text="gets stressed before travel", now=BASE - timedelta(days=9))
    emb = FakeEmbedder()
    assert embeddings.fill_missing_notes(conn, emb) == 7
    qvec = embeddings.embed_query(emb, "stressed")
    assert old not in [n["id"] for n in memory.list_active(conn, limit=3)]
    assert old in [n["id"] for n in memory.list_active(conn, limit=3, qvec=qvec)]


def test_extract_stores_facts_and_reuses_known_names(conn):
    a = add(conn, "Lunch with Sammy. Worried about the review.", 0)
    conn.execute("INSERT INTO people(name, aliases, created_at) VALUES ('Sam', '[\"Sammy\"]', 'x')")
    conn.commit()
    llm = FakeLLM([{"people": ["Sammy", 5], "events": ["lunch"], "worries": ["the review"], "junk": 1}])
    assert extract.fill_missing(conn, llm) == 1
    facts = extract.facts_for(conn, [a])[a]
    assert facts["people"] == ["Sam"] and facts["worries"] == ["the review"] and facts["wins"] == []
    assert "Sammy" in llm.calls[0]["user"]
    rows = conn.execute("SELECT * FROM entries").fetchall()
    assert extract.numbers(conn, rows)["people"] == [{"name": "Sam", "count": 1}]
    entries.update_entry(conn, a, text="changed", mood=None, energy=None)
    assert extract.missing_count(conn) == 1


def test_extract_stops_quietly_on_model_error(conn):
    add(conn, "one", 0)
    add(conn, "two", 1)
    llm = FakeLLM([LLMError("x", retryable=True, reason="timeout")])
    assert extract.fill_missing(conn, llm) == 0
    assert len(llm.calls) == 1


def test_new_note_retires_the_note_it_replaces(conn):
    old = memory.add_note(conn, kind="note", text="Prefers mornings", now=BASE)
    keep = memory.add_note(conn, kind="note", text="Likes tea", now=BASE)
    conv = conn.execute(
        "INSERT INTO conversations(started_at, last_message_at) VALUES ('a', 'b')").lastrowid
    conn.execute("INSERT INTO messages(conversation_id, role, text, cited_entry_ids, created_at)"
                 " VALUES (?, 'assistant', 'hi', '[4, 9]', 'b')", (conv,))
    conn.commit()
    llm = FakeLLM([{"notes": ["Now prefers evenings"], "replaces": [old, 999, "x"]}])
    result = chat_memory.summarize(llm, [{"role": "user", "text": "hi"}], chat_memory.active_notes(conn))
    assert result.replaces == (old,)
    assert f'<note id="{old}">' in llm.calls[0]["user"]
    assert chat_memory.store(conn, conv, result.notes, BASE + timedelta(hours=1), replaces=result.replaces) == 1
    row = conn.execute("SELECT deleted_at, superseded_by FROM ai_memory WHERE id = ?", (old,)).fetchone()
    new = conn.execute("SELECT id, source_entry_ids FROM ai_memory WHERE conversation_id = ?", (conv,)).fetchone()
    assert row["deleted_at"] is not None and row["superseded_by"] == new["id"]
    assert new["source_entry_ids"] == "[4, 9]"
    assert keep in [n["id"] for n in memory.list_active(conn)]


def test_pattern_evidence_is_verified_word_for_word(conn):
    a = add(conn, "I skipped lunch again and felt flat all afternoon", 0)
    rows = conn.execute("SELECT * FROM entries").fetchall()
    raw = {"patterns": [
        {"label": "Skipped meals", "kind": "risk", "text": "t",
         "evidence": [{"quote": "skipped lunch again and felt flat", "entry_id": a},
                      {"quote": "something never written here at all", "entry_id": a}]},
        {"label": "Made up", "kind": "risk", "text": "t",
         "evidence": [{"quote": "not in the entry text", "entry_id": a}]},
    ], "since_last": [{"item": "eat lunch", "status": "not_done", "note": "n"},
                      {"item": "x", "status": "weird"}, "bad"]}
    extra = reflect.weekly_extra(raw, rows)
    first, second = extra["patterns"]
    assert first["backed"] and len(first["evidence"]) == 1
    assert first["evidence"][0]["date"] == rows[0]["local_date"]
    assert second["backed"] is False and second["evidence"] == []
    assert [s["status"] for s in extra["since_last"]] == ["not_done", "unclear"]


def test_previous_report_feeds_the_next_prompt(conn):
    from app import periods, reflections
    reflections.save(conn, kind="weekly", period_key="2026-W40", period_start="2026-09-28",
                     period_end="2026-10-04", model="m", now=BASE,
                     content={"improve": [{"step": "sleep before midnight", "why": "w"}],
                              "worries": [{"worry": "the review"}], "focus": [{"id": 1, "text": "walk daily"}]})
    prev = reflect.previous_report(conn, "weekly", periods.week_of(date(2026, 10, 7)))
    text = prompts.weekly_user([], [], [], False, previous=prev,
                               counted={"entries": 3, "days": 2, "words": 40,
                                        "people": [{"name": "Sam", "count": 2}]})
    assert "sleep before midnight" in text and "the review" in text and "walk daily" in text
    assert "Sam (2)" in text and "3 entries on 2 days" in text
    assert reflect.previous_report(conn, "weekly", periods.week_of(date(2026, 9, 29))) is None
