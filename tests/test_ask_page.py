import pytest

from app import db
from app.llm import LLMError
from tests.fake_llm import FakeLLM
from tests.test_dashboard import add_entry, make_client, today_local

@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "ask.db")
    c = db.connect(path)
    db.init_db(c)
    c.close()
    return path


@pytest.fixture
def client(db_path):
    return make_client(db_path)


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)


TEXT = "Today I finally finished the mural for the cafe and felt really proud of it."
QUOTE = "finished the mural for the cafe and felt really proud"


def _today():
    return today_local().isoformat()


def _good(entry_id, quote=QUOTE):
    return {"found": True, "answer": "You finished the cafe mural and felt proud.",
            "citations": [{"entry_id": entry_id, "quote": quote}]}


@pytest.fixture
def seeded(db_path):
    return add_entry(db_path, _today(), text=TEXT)


def test_ask_page_renders_and_nav_link(client):
    r = client.get("/ask")
    assert r.status_code == 200
    assert "Answers come only from your entries" in r.text
    assert 'hx-post="/ask"' in r.text
    assert 'href="/ask" aria-current="page"' in r.text  # desktop nav marks it
    assert r.text.count('href="/ask"') == 1  # the mobile list hides the current page like the others
    assert 'href="/ask"' in client.get("/memory").text


def test_post_ask_returns_answer_with_date_and_quote(db_path, seeded):
    llm = FakeLLM([_good(seeded)])
    r = make_client(db_path, llm).post("/ask", data={"question": "mural cafe"})
    assert r.status_code == 200
    assert "You finished the cafe mural" in r.text
    assert QUOTE in r.text
    assert f'href="/entries/{seeded}"' in r.text
    assert "Evidence from your entries" in r.text
    assert f'datetime="{_today()}"' in r.text
    assert len(llm.calls) == 1


def test_post_blank_question_422(client):
    r = client.post("/ask", data={"question": "   "})
    assert r.status_code == 422
    assert 'id="error"' in r.text


def test_post_no_match_shows_not_in_journal_without_llm_call(db_path, seeded):
    llm = FakeLLM([])
    r = make_client(db_path, llm).post("/ask", data={"question": "zeppelin"})
    assert r.status_code == 200
    assert "Not in your journal" in r.text and "The journal does not mention this." in r.text
    assert llm.calls == []


def test_post_llm_down_shows_friendly_error(db_path, seeded):
    llm = FakeLLM([LLMError("down", retryable=True, reason="unreachable")])
    r = make_client(db_path, llm).post("/ask", data={"question": "mural cafe"})
    assert r.status_code == 200
    assert 'data-reason="unreachable"' in r.text
    assert "could not reach the model" in r.text
    assert "Evidence" not in r.text


def test_no_key_without_injected_llm_is_friendly(db_path, seeded):
    r = make_client(db_path).post("/ask", data={"question": "mural cafe"})
    assert r.status_code == 200
    assert 'data-reason="no_key"' in r.text


def test_cross_site_post_rejected(db_path, seeded):
    llm = FakeLLM([_good(seeded)])
    r = make_client(db_path, llm).post("/ask", data={"question": "mural cafe"},
                                       headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert llm.calls == []


def test_answer_html_escapes_quote(db_path):
    evil = "I wrote <script>alert(1)</script> on the wall of the cafe today."
    eid = add_entry(db_path, _today(), text=evil)
    q = "<script>alert(1)</script> on the wall"
    llm = FakeLLM([{"found": True, "answer": "You wrote <b>on</b> the wall.",
                    "citations": [{"entry_id": eid, "quote": q}]}])
    r = make_client(db_path, llm).post("/ask", data={"question": "wall cafe"})
    assert "<script>alert(1)" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "<b>on</b>" not in r.text


def test_page_has_busy_and_error_hooks(client):
    html = client.get("/ask").text
    assert "htmx:responseError" in html and "htmx:sendError" in html
    assert 'aria-busy="false"' in html and 'id="ask-live"' in html
    assert 'hx-disabled-elt="#ask-go"' in html and "#ask-q" not in html.split("hx-disabled-elt=")[1].split(">")[0]
    assert "innerHTML" not in html.split("<script nonce=")[-1]
