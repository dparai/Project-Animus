from datetime import datetime, timezone

import pytest

from app import counselor, db
from app.llm import LLMError
from tests.fake_llm import FakeLLM
from tests.test_dashboard import add_entry, make_client, today_local


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "chat.db")
    c = db.connect(path)
    db.init_db(c)
    c.close()
    return path


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)


def q(db_path, sql, params=()):
    c = db.connect(db_path)
    try:
        return c.execute(sql, params).fetchall()
    finally:
        c.close()


def new_conv(db_path):
    c = db.connect(db_path)
    try:
        return counselor.start(c, datetime.now(timezone.utc))
    finally:
        c.close()


def roles(db_path, cid):
    return [r["role"] for r in q(db_path, "SELECT role FROM messages WHERE conversation_id = ? ORDER BY id", (cid,))]


def test_counselor_page_empty_state(db_path):
    r = make_client(db_path).get("/counselor")
    assert r.status_code == 200
    assert "Start a conversation" in r.text
    assert 'action="/counselor/new"' in r.text
    assert 'href="/counselor" aria-current="page"' in r.text
    assert "It is not a therapist." in r.text


def test_nav_link_on_other_pages(db_path):
    assert 'href="/counselor"' in make_client(db_path).get("/memory").text


def test_new_conversation_and_first_message_roundtrip(db_path):
    eid = add_entry(db_path, today_local().isoformat(), text="Walked by the river and felt calm today.")
    llm = FakeLLM([{"reply": "What made the river feel calm?", "cited_entry_ids": [eid]}])
    client = make_client(db_path, llm)
    r = client.post("/counselor/new", follow_redirects=False)
    assert r.status_code == 303
    cid = int(r.headers["location"].rsplit("/", 1)[1])
    page = client.get(f"/counselor/{cid}")
    assert page.status_code == 200 and 'hx-post="/counselor/%d/message"' % cid in page.text
    assert "Counselor reads a few relevant entries and your memory notes. It is not a therapist." in page.text
    r = client.post(f"/counselor/{cid}/message", data={"text": "I walked by the river"})
    assert r.status_code == 200
    assert "I walked by the river" in r.text and "What made the river feel calm?" in r.text
    assert f'href="/entries/{eid}"' in r.text
    assert roles(db_path, cid) == ["user", "assistant"]
    assert len(llm.calls) == 1
    assert client.get("/counselor").text.count("What made the river feel calm?") == 1  # latest open conversation


def test_model_error_keeps_user_message_and_shows_notice(db_path):
    cid = new_conv(db_path)
    llm = FakeLLM([LLMError("x", retryable=True, reason="timeout")])
    client = make_client(db_path, llm)
    r = client.post(f"/counselor/{cid}/message", data={"text": "hello there"})
    assert r.status_code == 200
    assert "hello there" in r.text
    assert counselor.error_text("timeout") in r.text
    assert "Try again" in r.text and f'hx-post="/counselor/{cid}/retry"' in r.text
    assert roles(db_path, cid) == ["user"]
    # reload still offers the retry
    assert "Try again" in client.get(f"/counselor/{cid}").text


def test_retry_does_not_duplicate_message(db_path):
    cid = new_conv(db_path)
    llm = FakeLLM([LLMError("x", retryable=True, reason="timeout"), {"reply": "I am listening.", "cited_entry_ids": []}])
    client = make_client(db_path, llm)
    client.post(f"/counselor/{cid}/message", data={"text": "hello there"})
    r = client.post(f"/counselor/{cid}/retry")
    assert r.status_code == 200 and "I am listening." in r.text
    assert roles(db_path, cid) == ["user", "assistant"]
    assert "Try again" not in r.text


def test_retry_rejected_when_nothing_pending_or_ended(db_path):
    cid = new_conv(db_path)
    llm = FakeLLM([{"reply": "Tell me more.", "cited_entry_ids": []}])
    client = make_client(db_path, llm)
    assert client.post(f"/counselor/{cid}/retry").status_code == 409  # empty conversation
    client.post(f"/counselor/{cid}/message", data={"text": "hi"})
    r = client.post(f"/counselor/{cid}/retry")  # last message is the assistant's
    assert r.status_code == 409 and "nothing waiting" in r.text
    assert len(llm.calls) == 1 and roles(db_path, cid) == ["user", "assistant"]
    cid2 = new_conv(db_path)
    c = db.connect(db_path)
    counselor.add_user_message(c, cid2, "pending", datetime.now(timezone.utc))
    counselor.end(c, cid2, datetime.now(timezone.utc))
    c.close()
    assert client.post(f"/counselor/{cid2}/retry").status_code == 409
    assert len(llm.calls) == 1


def test_in_flight_guard_blocks_second_request(db_path):
    cid = new_conv(db_path)
    llm = FakeLLM([{"reply": "Tell me more.", "cited_entry_ids": []}])
    client = make_client(db_path, llm)
    client.app.state.chat_busy.add(cid)
    r = client.post(f"/counselor/{cid}/message", data={"text": "second"})
    assert r.status_code == 409 and "already on its way" in r.text
    assert llm.calls == [] and roles(db_path, cid) == []  # no model call, no duplicate message
    client.app.state.chat_busy.discard(cid)
    assert client.post(f"/counselor/{cid}/message", data={"text": "ok"}).status_code == 200
    assert cid not in client.app.state.chat_busy  # released after the turn


def test_guard_released_after_model_error(db_path):
    cid = new_conv(db_path)
    client = make_client(db_path, FakeLLM([LLMError("x", retryable=False, reason="unreachable")]))
    client.post(f"/counselor/{cid}/message", data={"text": "hi"})
    assert cid not in client.app.state.chat_busy


def test_deleted_entry_citation_skipped_on_render(db_path):
    keep = add_entry(db_path, today_local().isoformat(), text="Kept entry about the river.")
    gone = add_entry(db_path, today_local().isoformat(), text="Gone entry about the river.")
    cid = new_conv(db_path)
    c = db.connect(db_path)
    counselor._insert(c, cid, "user", "river", (), datetime.now(timezone.utc))
    counselor._insert(c, cid, "assistant", "The river again.", (keep, gone, 9999), datetime.now(timezone.utc))
    c.execute("DELETE FROM entries WHERE id = ?", (gone,))
    c.commit()
    c.close()
    r = make_client(db_path).get(f"/counselor/{cid}")
    assert r.status_code == 200 and "The river again." in r.text
    assert f'href="/entries/{keep}"' in r.text
    assert f'href="/entries/{gone}"' not in r.text and 'href="/entries/9999"' not in r.text


def test_end_marks_conversation_ended_and_blocks_messages(db_path):
    cid = new_conv(db_path)
    llm = FakeLLM([])
    client = make_client(db_path, llm)
    r = client.post(f"/counselor/{cid}/end")
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/counselor/{cid}"
    assert q(db_path, "SELECT ended_at FROM conversations WHERE id = ?", (cid,))[0][0] is not None
    page = client.get(f"/counselor/{cid}")
    assert "This conversation has ended" in page.text and 'id="chat-form"' not in page.text
    r = client.post(f"/counselor/{cid}/message", data={"text": "more"})
    assert r.status_code == 409 and roles(db_path, cid) == [] and llm.calls == []
    assert "Start a conversation" in client.get("/counselor").text  # no open conversation left


def test_delete_conversation(db_path):
    cid = new_conv(db_path)
    client = make_client(db_path, FakeLLM([{"reply": "Hi.", "cited_entry_ids": []}]))
    client.post(f"/counselor/{cid}/message", data={"text": "hello"})
    r = client.delete(f"/counselor/{cid}")
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/counselor"
    assert q(db_path, "SELECT COUNT(*) FROM conversations")[0][0] == 0
    assert q(db_path, "SELECT COUNT(*) FROM messages")[0][0] == 0
    assert client.delete(f"/counselor/{cid}").status_code == 404


def test_unknown_conversation_404(db_path):
    client = make_client(db_path, FakeLLM([]))
    assert client.get("/counselor/999").status_code == 404
    assert client.post("/counselor/999/message", data={"text": "x"}).status_code == 404
    assert client.post("/counselor/999/retry").status_code == 404
    assert client.post("/counselor/999/end").status_code == 404
    assert client.delete("/counselor/999").status_code == 404


def test_blank_message_422_nothing_saved(db_path):
    cid = new_conv(db_path)
    llm = FakeLLM([])
    r = make_client(db_path, llm).post(f"/counselor/{cid}/message", data={"text": "   "})
    assert r.status_code == 422 and "Write a message first" in r.text
    assert roles(db_path, cid) == [] and llm.calls == []


def test_message_html_escaped(db_path):
    cid = new_conv(db_path)
    payload = "<script>alert(1)</script> & <b>bold</b>"
    llm = FakeLLM([{"reply": "<img src=x onerror=alert(2)> ok", "cited_entry_ids": []}])
    r = make_client(db_path, llm).post(f"/counselor/{cid}/message", data={"text": payload})
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "<img src=x" not in r.text and "&lt;img src=x" in r.text
    page = make_client(db_path).get(f"/counselor/{cid}")
    assert "<script>alert(1)" not in page.text and "<b>bold</b>" not in page.text


def test_cross_site_post_rejected(db_path):
    cid = new_conv(db_path)
    llm = FakeLLM([])
    client = make_client(db_path, llm)
    bad = {"Origin": "https://evil.example"}
    assert client.post("/counselor/new", headers=bad).status_code == 403
    assert client.post(f"/counselor/{cid}/message", data={"text": "x"}, headers=bad).status_code == 403
    assert client.post(f"/counselor/{cid}/retry", headers=bad).status_code == 403
    assert client.post(f"/counselor/{cid}/end", headers=bad).status_code == 403
    assert client.delete(f"/counselor/{cid}", headers=bad).status_code == 403
    assert roles(db_path, cid) == [] and llm.calls == []
    assert q(db_path, "SELECT COUNT(*) FROM conversations")[0][0] == 1


def test_list_shows_conversations(db_path):
    a = new_conv(db_path)
    b = new_conv(db_path)
    text = make_client(db_path).get(f"/counselor/{a}").text
    assert f'href="/counselor/{a}" aria-current="page"' in text and f'href="/counselor/{b}"' in text


def test_busy_notice_has_no_retry_button(db_path):
    cid = new_conv(db_path)
    c = db.connect(db_path)
    counselor.add_user_message(c, cid, "waiting", datetime.now(timezone.utc))
    c.close()
    client = make_client(db_path, FakeLLM([]))
    client.app.state.chat_busy.add(cid)
    r = client.post(f"/counselor/{cid}/retry")
    assert r.status_code == 409 and "already on its way" in r.text and "Try again" not in r.text


@pytest.mark.parametrize("bad", ["abc", "9" * 25, "0", "-1"])
def test_non_integer_or_huge_ids_are_404(db_path, bad):
    client = make_client(db_path, FakeLLM([]))
    assert client.get(f"/counselor/{bad}").status_code == 404
    assert client.post(f"/counselor/{bad}/message", data={"text": "x"}).status_code == 404
    assert client.post(f"/counselor/{bad}/retry").status_code == 404
    assert client.post(f"/counselor/{bad}/end").status_code == 404
    assert client.delete(f"/counselor/{bad}").status_code == 404


def test_message_to_conversation_deleted_midway_is_404(db_path, monkeypatch):
    cid = new_conv(db_path)
    client = make_client(db_path, FakeLLM([]))

    def gone(*a, **k):
        raise ValueError("conversation")

    monkeypatch.setattr(counselor, "add_user_message", gone)
    r = client.post(f"/counselor/{cid}/message", data={"text": "hello"})
    assert r.status_code == 404 and "Write a message first" not in r.text
    assert cid not in client.app.state.chat_busy


def test_get_conversation_safe_for_odd_ids(db_path):
    c = db.connect(db_path)
    try:
        assert counselor.get_conversation(c, 10**30) is None
        assert counselor.get_conversation(c, "1") is None
        assert counselor.get_conversation(c, 0) is None
    finally:
        c.close()
