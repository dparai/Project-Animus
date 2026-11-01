import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app import chat_memory, counselor, db, jobs, memory, periods, reflect
from app.db import connect
from app.llm import LLMError
from tests.fake_llm import FakeLLM
from tests.test_dashboard import make_client

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)
FMT = "%Y-%m-%dT%H:%M:%SZ"
SECRET = "fake chat text that must never be logged"


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)


def chat(conn, user_messages, at, *, ended=False, replies=True):
    """A fake conversation: `user_messages` owner messages (each followed by a reply)."""
    cid = counselor.start(conn, at)
    for i in range(user_messages):
        counselor._insert(conn, cid, "user", f"{SECRET} {i}", (), at)
        if replies:
            counselor._insert(conn, cid, "assistant", f"fake reply {i}", (), at)
    if ended:
        counselor.end(conn, cid, at)
    return cid


def summarized_at(conn, cid):
    return conn.execute("SELECT summarized_at FROM conversations WHERE id = ?", (cid,)).fetchone()[0]


def active_notes(conn):
    return conn.execute(
        "SELECT * FROM ai_memory WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()


def all_notes(conn):
    return conn.execute("SELECT * FROM ai_memory ORDER BY id").fetchall()


# --- due conversations ----------------------------------------------------------------------

def test_due_requires_two_user_messages(conn):
    old = NOW - timedelta(hours=7)
    one = chat(conn, 1, old)
    two = chat(conn, 2, old)
    one_ended_fresh = chat(conn, 1, NOW, ended=True)
    one_open_fresh = chat(conn, 1, NOW)
    assert chat_memory.due_conversations(conn, NOW) == [two]
    # Too short to summarize but finished: marked so it stops being a candidate, no notes made.
    assert summarized_at(conn, one) == NOW.strftime(FMT)
    assert summarized_at(conn, one_ended_fresh) == NOW.strftime(FMT)
    # Still open and recent: the owner may keep talking, so it is left alone.
    assert summarized_at(conn, one_open_fresh) is None
    assert summarized_at(conn, two) is None
    assert all_notes(conn) == []


def test_due_after_end_or_six_hours_idle(conn):
    fresh = chat(conn, 3, NOW - timedelta(hours=5, minutes=59))
    idle = chat(conn, 2, NOW - timedelta(hours=6, minutes=1))
    ended = chat(conn, 2, NOW - timedelta(minutes=5), ended=True)
    done = chat(conn, 2, NOW - timedelta(hours=8))
    conn.execute("UPDATE conversations SET summarized_at = ? WHERE id = ?", (NOW.strftime(FMT), done))
    conn.commit()
    assert chat_memory.due_conversations(conn, NOW) == [idle, ended]
    assert fresh not in chat_memory.due_conversations(conn, NOW + timedelta(seconds=30))
    assert fresh in chat_memory.due_conversations(conn, NOW + timedelta(minutes=1, seconds=1))


# --- summarize (pure) -----------------------------------------------------------------------

MSGS = [{"role": "user", "text": "first <b>message</b>"}, {"role": "assistant", "text": "a reply"},
        {"role": "user", "text": "second message"}]


def test_summary_caps_notes_and_length():
    long_note = "Walks help when work stress builds " * 20
    llm = FakeLLM([{"notes": [
        "  Prefers   short\n questions  ", long_note, 42, None, "", "   ",
        "Prefers short questions", "Music calms them down", "Fourth note dropped",
    ]}])
    res = chat_memory.summarize(llm, MSGS)
    assert res.ok and res.reason == ""
    assert len(res.notes) == 3
    assert res.notes[0] == "Prefers short questions"
    assert len(res.notes[1]) <= chat_memory.NOTE_MAX_CHARS
    assert "\n" not in res.notes[1]
    assert res.notes[2] == "Music calms them down"
    # Chat text is escaped data in the prompt, and the rules are stated.
    call = llm.calls[0]
    assert "first &lt;b&gt;message&lt;/b&gt;" in call["user"]
    assert "<b>" not in call["user"]
    for rule in ("DATA", "240", "diagnos", "medical", "third part"):
        assert rule in call["system"]


def test_summary_empty_list_is_valid_and_bad_shape_is_not():
    assert chat_memory.summarize(FakeLLM([{"notes": []}]), MSGS).ok
    assert chat_memory.summarize(FakeLLM([{"notes": []}]), MSGS).notes == ()
    bad = chat_memory.summarize(FakeLLM([{"reply": "x"}]), MSGS)
    assert not bad.ok and bad.reason == "invalid_output"
    err = chat_memory.summarize(FakeLLM([LLMError("down", retryable=True, reason="timeout")]), MSGS)
    assert not err.ok and err.reason == "timeout"


# --- store ---------------------------------------------------------------------------------

def test_store_is_atomic(conn):
    cid = chat(conn, 2, NOW - timedelta(hours=7))
    # Second insert fails: neither the first note nor summarized_at may survive.
    conn.executescript(
        "CREATE TRIGGER boom BEFORE INSERT ON ai_memory WHEN (SELECT COUNT(*) FROM ai_memory) >= 1"
        " BEGIN SELECT RAISE(ABORT, 'boom'); END;"
    )
    with pytest.raises(sqlite3.DatabaseError):
        chat_memory.store(conn, cid, ("note one", "note two"), NOW)
    assert all_notes(conn) == []
    assert summarized_at(conn, cid) is None
    conn.executescript("DROP TRIGGER boom;")
    assert chat_memory.store(conn, cid, ("note one", "note two"), NOW) == 2
    rows = all_notes(conn)
    assert [(r["kind"], r["text"], r["conversation_id"]) for r in rows] == [
        ("note", "note one", cid), ("note", "note two", cid)]
    assert summarized_at(conn, cid) == NOW.strftime(FMT)
    # A second store for the same conversation adds nothing.
    assert chat_memory.store(conn, cid, ("note three",), NOW) == 0
    assert len(all_notes(conn)) == 2


def test_store_with_no_notes_still_marks_summarized(conn):
    cid = chat(conn, 2, NOW - timedelta(hours=7))
    assert chat_memory.store(conn, cid, (), NOW) == 0
    assert summarized_at(conn, cid) == NOW.strftime(FMT)
    assert chat_memory.due_conversations(conn, NOW) == []


# --- scheduler step ---------------------------------------------------------------------------

def test_llm_error_leaves_conversation_unsummarized(conn, tmp_path, caplog):
    path = str(tmp_path / "test.db")
    cid = chat(conn, 2, NOW - timedelta(hours=7))
    caplog.set_level(logging.INFO, logger="animus")
    llm = FakeLLM([LLMError("down", retryable=True, reason="timeout"), {"notes": ["Likes walks"]}])
    assert chat_memory.run_step(path, llm, NOW) == []
    assert summarized_at(conn, cid) is None
    assert all_notes(conn) == []
    assert "reason=timeout" in caplog.text
    # Not retried within the hour, then retried on a later tick.
    assert chat_memory.run_step(path, llm, NOW + timedelta(minutes=5)) == []
    assert len(llm.calls) == 1
    assert chat_memory.run_step(path, llm, NOW + timedelta(minutes=61)) == [cid]
    assert [r["text"] for r in active_notes(conn)] == ["Likes walks"]
    assert summarized_at(conn, cid) is not None
    assert SECRET not in caplog.text and "Likes walks" not in caplog.text


def test_step_processes_at_most_three_per_tick(conn, tmp_path):
    path = str(tmp_path / "test.db")
    ids = [chat(conn, 2, NOW - timedelta(hours=10, minutes=i)) for i in range(5)]
    llm = FakeLLM([{"notes": []}] * 5)
    first = chat_memory.run_step(path, llm, NOW)
    assert len(first) == 3 and len(llm.calls) == 3
    assert {c["read_timeout"] for c in llm.calls} == {120.0}
    second = chat_memory.run_step(path, llm, NOW)
    assert sorted(first + second) == sorted(ids)


def test_merge_counts_toward_three_calls_per_tick(conn, tmp_path):
    path = str(tmp_path / "test.db")
    seed_notes(conn, chat_memory.NOTE_CAP + 1)
    for i in range(3):
        chat(conn, 2, NOW - timedelta(hours=10, minutes=i))
    llm = FakeLLM([{"notes": []}] * 3 + [{"notes": ["merged"]}])
    chat_memory.run_step(path, llm, NOW)
    assert len(llm.calls) == 3  # no merge after three summaries
    chat_memory.run_step(path, llm, NOW)
    assert len(llm.calls) == 4 and llm.calls[3]["read_timeout"] == 120.0
    assert "merged" in [r["text"] for r in active_notes(conn)]


class Garbage(FakeLLM):
    def complete_json(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user})
        return {"nonsense": True}


def attempts(conn, cid):
    return conn.execute("SELECT summary_attempts FROM conversations WHERE id = ?", (cid,)).fetchone()[0]


def test_failures_do_not_starve_others_and_give_up_after_three(conn, tmp_path, caplog):
    path = str(tmp_path / "test.db")
    caplog.set_level(logging.INFO, logger="animus")
    ids = [chat(conn, 2, NOW - timedelta(hours=10, minutes=10 - i)) for i in range(4)]
    llm = Garbage()
    assert chat_memory.run_step(path, llm, NOW) == []
    assert len(llm.calls) == 3  # at most 3 model calls per tick
    assert [attempts(conn, c) for c in ids] == [1, 1, 1, 0]
    # The failed three are cooling down, so chat 4 gets its turn on the next tick.
    assert chat_memory.run_step(path, llm, NOW + timedelta(minutes=30)) == []
    assert len(llm.calls) == 4
    assert [attempts(conn, c) for c in ids] == [1, 1, 1, 1]
    assert chat_memory.run_step(path, llm, NOW + timedelta(minutes=45)) == []
    assert len(llm.calls) == 4  # cooldown: nothing retried within the hour
    for minutes in (61, 91, 122):
        chat_memory.run_step(path, llm, NOW + timedelta(minutes=minutes))
    assert summarized_at(conn, ids[3]) is None and attempts(conn, ids[3]) == 2
    chat_memory.run_step(path, llm, NOW + timedelta(minutes=152))
    assert len(llm.calls) == 12
    assert [attempts(conn, c) for c in ids] == [3, 3, 3, 3]
    assert all(summarized_at(conn, c) is not None for c in ids)
    assert all_notes(conn) == []
    assert "status=gave_up reason=invalid_output" in caplog.text
    assert chat_memory.run_step(path, llm, NOW + timedelta(hours=10)) == []
    assert len(llm.calls) == 12


def test_newer_message_during_call_skips_store(conn, tmp_path):
    path = str(tmp_path / "test.db")
    cid = chat(conn, 2, NOW - timedelta(hours=7))

    class Interrupted(FakeLLM):
        def complete_json(self, system, user, **kwargs):
            other = connect(path)
            counselor._insert(other, cid, "user", "fake late message", (), NOW)
            other.close()
            return super().complete_json(system, user, **kwargs)

    assert chat_memory.run_step(path, Interrupted([{"notes": ["Too early"]}]), NOW) == []
    assert summarized_at(conn, cid) is None
    assert all_notes(conn) == []


def test_resumed_chat_is_resummarized_and_old_notes_replaced(conn, tmp_path):
    path = str(tmp_path / "test.db")
    cid = chat(conn, 2, NOW - timedelta(hours=7))
    assert chat_memory.run_step(path, FakeLLM([{"notes": ["Old note"]}]), NOW) == [cid]
    later = NOW + timedelta(hours=1)
    counselor.add_user_message(conn, cid, "fake resumed message", later)
    assert summarized_at(conn, cid) is None
    assert chat_memory.due_conversations(conn, later) == []  # waits until idle again
    again = later + timedelta(hours=7)
    assert chat_memory.run_step(path, FakeLLM([{"notes": ["New note"]}]), again) == [cid]
    assert [r["text"] for r in active_notes(conn)] == ["New note"]
    old = [r for r in all_notes(conn) if r["text"] == "Old note"]
    assert len(old) == 1 and old[0]["deleted_at"] is not None
    # Assistant replies do not reopen a summarized chat.
    counselor._insert(conn, cid, "assistant", "fake reply", (), again)
    assert summarized_at(conn, cid) is not None


def test_no_key_stops_step_without_marking(tmp_path):
    path = str(tmp_path / "nokey.db")
    c = connect(path)
    db.init_db(c)
    cid = chat(c, 2, NOW - timedelta(hours=7))
    c.close()
    assert chat_memory.run_step(path, None, NOW) == []  # real client, no key configured
    c = connect(path)
    assert summarized_at(c, cid) is None
    c.close()


def test_scheduler_tick_runs_memory_after_reflections_and_survives_failure(conn, tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    cid = chat(conn, 2, NOW - timedelta(hours=7))
    llm = FakeLLM([{"notes": ["Journals at night"]}])
    jobs.scheduler_tick(path, llm, NOW)  # no entries: reflection jobs are skipped without a call
    assert [r["text"] for r in active_notes(conn)] == ["Journals at night"]
    assert summarized_at(conn, cid) is not None

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(chat_memory, "run_step", broken)
    jobs.scheduler_tick(path, FakeLLM(), NOW + timedelta(days=7))  # must not raise


def test_notes_flow_into_next_weekly_prompt(conn):
    cid = chat(conn, 2, NOW - timedelta(hours=7))
    res = chat_memory.summarize(FakeLLM([{"notes": ["Feels calmer after evening walks"]}]), MSGS)
    chat_memory.store(conn, cid, res.notes, NOW)
    assert "Feels calmer after evening walks" in [r["text"] for r in memory.list_active(conn)]
    conn.execute(
        "INSERT INTO entries(created_at, local_date, text) VALUES (?, ?, ?)",
        ("2026-11-03T12:00:00Z", "2026-11-03", "Finished the first draft of the song and it sounds honest."),
    )
    conn.commit()
    eid = conn.execute("SELECT id FROM entries").fetchone()[0]
    llm = FakeLLM([{"wins": [{"id": "w1", "text": "You finished a draft.", "quote": "first draft of the song",
                              "entry_id": eid}], "tough_love": [], "focus": [], "focus_review": []}])
    reflect.generate_weekly(conn, llm, periods.week_of(date(2026, 11, 3)), NOW + timedelta(days=6))
    assert "Feels calmer after evening walks" in llm.calls[0]["user"]


# --- consolidation --------------------------------------------------------------------------

def seed_notes(conn, count, kind="note"):
    ids = []
    for i in range(count):
        ids.append(memory.add_note(conn, kind=kind, text=f"{kind} {i}", now=NOW + timedelta(minutes=i)))
    return ids


CAP, BATCH, MAX = chat_memory.NOTE_CAP, chat_memory.MERGE_BATCH, chat_memory.MERGE_MAX


def test_merge_constants_fit_the_prompt_window():
    assert (CAP, BATCH, MAX) == (30, 15, 5)
    assert CAP <= memory.PROMPT_LIMIT


def test_consolidation_merges_oldest_and_keeps_originals_deleted_not_lost(conn):
    corrections = [memory.add_note(conn, kind="correction", text="correction old", now=NOW - timedelta(days=9))]
    ids = seed_notes(conn, CAP + 1)
    later = NOW + timedelta(days=1)
    llm = FakeLLM([{"notes": ["merged A", "merged B", "merged A", 7]}])
    assert chat_memory.consolidate_if_needed(conn, llm, later) is True
    # The prompt holds the oldest BATCH 'note' notes only, as escaped data.
    user = llm.calls[0]["user"]
    assert "note 0<" in user and f"note {BATCH - 1}<" in user and f"note {BATCH}<" not in user
    assert "correction old" not in user
    rows = {r["id"]: r for r in all_notes(conn)}
    for nid in ids[:BATCH]:
        assert rows[nid]["deleted_at"] == later.strftime(FMT)  # soft deleted, still in the table
    for nid in ids[BATCH:] + corrections:
        assert rows[nid]["deleted_at"] is None
    merged = [r for r in active_notes(conn) if r["text"].startswith("merged")]
    assert [(r["kind"], r["text"]) for r in merged] == [("note", "merged A"), ("note", "merged B")]
    assert len(all_notes(conn)) == 1 + CAP + 1 + 2
    # Now under the cap: no further call.
    assert chat_memory.consolidate_if_needed(conn, FakeLLM(), later) is False


def test_merged_notes_keep_newest_source_date(conn):
    ids = seed_notes(conn, CAP + 1)  # note i at NOW + i minutes
    correction = memory.add_note(conn, kind="correction", text="later correction",
                                 now=NOW + timedelta(minutes=BATCH - 1, seconds=30))
    newer_note = ids[BATCH]  # created after the correction
    later = NOW + timedelta(days=1)
    assert chat_memory.consolidate_if_needed(conn, FakeLLM([{"notes": ["merged"]}]), later)
    merged = [r for r in active_notes(conn) if r["text"] == "merged"]
    source = conn.execute("SELECT created_at FROM ai_memory WHERE id = ?", (ids[BATCH - 1],)).fetchone()[0]
    assert [r["created_at"] for r in merged] == [source]
    order = [r["id"] for r in memory.list_all_active(conn, 200)]  # plain newest-first order
    assert order.index(newer_note) < order.index(correction) < order.index(merged[0]["id"])
    prompt_order = [r["id"] for r in memory.list_active(conn)]
    assert prompt_order.index(correction) < prompt_order.index(merged[0]["id"])


def test_merge_failure_cooldown(conn):
    seed_notes(conn, CAP + 1)
    t = NOW + timedelta(days=1)
    assert chat_memory.consolidate_if_needed(conn, FakeLLM([{"notes": []}]), t) is False
    idle = FakeLLM()
    assert chat_memory.consolidate_if_needed(conn, idle, t + timedelta(minutes=59)) is False
    assert idle.calls == []
    retry = FakeLLM([{"notes": ["merged"]}])
    assert chat_memory.consolidate_if_needed(conn, retry, t + timedelta(minutes=61)) is True
    assert len(retry.calls) == 1
    assert conn.execute("SELECT 1 FROM settings WHERE key = ?", (chat_memory.MERGE_FAILED_KEY,)).fetchone() is None


def test_prompts_label_conversation_notes_below_corrections(conn):
    from app import prompts
    cid = chat(conn, 2, NOW - timedelta(hours=7))
    chat_memory.store(conn, cid, ("Chat <derived> note",), NOW)
    memory.add_note(conn, kind="correction", text="A correction", now=NOW)
    notes = memory.list_active(conn)
    weekly = prompts.weekly_user([], notes, [], False)
    assert "(conversation, 2026-11-02) Chat &lt;derived&gt; note" in weekly
    assert "(correction, 2026-11-02) A correction" in weekly
    counsel = prompts.counselor_user([{"role": "user", "text": "hi"}], [], notes, False)
    assert '<note kind="conversation" date="2026-11-02">Chat &lt;derived&gt; note</note>' in counsel
    for system in (prompts.weekly_system(), prompts.monthly_system(), prompts.counselor_system()):
        assert "corrections outrank" in system


def test_counselor_prepare_labels_conversation_notes(conn):
    from app import prompts
    old = chat(conn, 2, NOW - timedelta(hours=7))
    chat_memory.store(conn, old, ("Chat derived note",), NOW)
    memory.add_note(conn, kind="note", text="Plain note", now=NOW)
    cid = counselor.start(conn, NOW)
    counselor.add_user_message(conn, cid, "fake question", NOW)
    ctx = counselor.prepare(conn, cid, date(2026, 11, 2))
    user = prompts.counselor_user(ctx.history, ctx.entries, ctx.notes, ctx.low_mood)
    assert '<note kind="conversation" date="2026-11-02">Chat derived note</note>' in user
    assert '<note kind="note" date="2026-11-02">Plain note</note>' in user


def test_consolidation_not_triggered_at_cap_or_by_corrections(conn):
    seed_notes(conn, CAP)
    seed_notes(conn, 20, kind="correction")
    llm = FakeLLM()
    assert chat_memory.consolidate_if_needed(conn, llm, NOW) is False
    assert llm.calls == []


@pytest.mark.parametrize("response", [
    LLMError("down", retryable=True, reason="timeout"),
    {"notes": []},
    {"notes": "not a list"},
    {"other": 1},
    {"notes": [f"n{i}" for i in range(MAX + 1)]},
])
def test_consolidation_failure_changes_nothing(conn, response):
    seed_notes(conn, CAP + 5)
    before = [tuple(r) for r in all_notes(conn)]
    assert chat_memory.consolidate_if_needed(conn, FakeLLM([response]), NOW + timedelta(days=1)) is False
    assert [tuple(r) for r in all_notes(conn)] == before


# --- migration and memory page ----------------------------------------------------------------

def test_migration_adds_conversation_id_to_existing_db(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript(
        "CREATE TABLE ai_memory (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " kind TEXT NOT NULL CHECK (kind IN ('correction','note')), text TEXT NOT NULL,"
        " reflection_id INTEGER NULL, claim_id TEXT NULL, created_at TEXT NOT NULL, deleted_at TEXT NULL);"
        "INSERT INTO ai_memory(kind, text, created_at) VALUES ('correction', 'kept', '2026-10-01T00:00:00Z');"
        "CREATE TABLE conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,"
        " last_message_at TEXT NOT NULL, ended_at TEXT NULL, summarized_at TEXT NULL);"
        "INSERT INTO conversations(started_at, last_message_at) VALUES ('2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z');"
    )
    c.commit()
    c.close()
    c = connect(path)
    db.init_db(c)
    db.init_db(c)  # idempotent
    cols = {r[1] for r in c.execute("PRAGMA table_info(ai_memory)")}
    assert "conversation_id" in cols
    row = c.execute("SELECT text, conversation_id FROM ai_memory").fetchone()
    assert tuple(row) == ("kept", None)
    conv = c.execute("SELECT summary_attempts, last_attempt_at FROM conversations").fetchone()
    assert tuple(conv) == (0, None)
    c.close()


def test_fresh_schema_has_conversation_id(conn):
    assert "conversation_id" in {r[1] for r in conn.execute("PRAGMA table_info(ai_memory)")}
    nid = memory.add_note(conn, kind="note", text="x", conversation_id=None, now=NOW)
    assert conn.execute("SELECT conversation_id FROM ai_memory WHERE id = ?", (nid,)).fetchone()[0] is None


def test_memory_page_shows_conversation_source_label_and_delete_works(tmp_path):
    path = str(tmp_path / "page.db")
    client = make_client(path)
    c = connect(path)
    started = datetime(2026, 10, 20, 18, 0, 0, tzinfo=timezone.utc)  # 11:00 PDT, 20 October
    cid = chat(c, 2, started)
    chat_memory.store(c, cid, ("Likes being asked one question at a time",), NOW)
    plain = memory.add_note(c, kind="correction", text="plain correction", now=NOW)
    nid = c.execute("SELECT id FROM ai_memory WHERE conversation_id = ?", (cid,)).fetchone()[0]
    c.close()
    html = client.get("/memory").text
    assert "from a conversation, Tuesday 20 October" in html
    assert html.count("from a conversation") == 1
    assert SECRET not in html  # message text is never shown on the Memory page
    assert client.delete(f"/memory/{nid}").status_code == 200
    html = client.get("/memory").text
    assert "Likes being asked" not in html and "plain correction" in html
    assert client.delete(f"/memory/{plain}").status_code == 200


def test_deleting_conversation_removes_its_notes(tmp_path):
    path = str(tmp_path / "gone.db")
    client = make_client(path)
    c = connect(path)
    cid = chat(c, 2, NOW - timedelta(days=1))
    chat_memory.store(c, cid, ("Kept after the chat is gone",), NOW)
    memory.add_note(c, kind="note", text="Unrelated note", now=NOW)
    keep = chat(c, 1, NOW)
    counselor.delete(c, cid)
    rows = {r["text"]: r for r in all_notes(c)}
    c.close()
    assert rows["Kept after the chat is gone"]["deleted_at"] is not None  # soft deleted, not lost
    assert rows["Unrelated note"]["deleted_at"] is None
    html = client.get("/memory").text
    assert "Kept after the chat is gone" not in html
    assert "Unrelated note" in html
    page = client.get(f"/counselor/{keep}").text
    assert "The memory notes made from it are removed too." in page
