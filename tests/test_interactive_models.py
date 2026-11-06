import json
from datetime import date, datetime, timezone

import httpx
import pytest

from app import ask, chat_memory, config, counselor, prompts
from app.config import LlmSettings
from app.llm import LLMError, LlmClient, interactive_models
from tests.fake_llm import FakeLLM

FAST, BIG = "nvidia/nemotron-3.5-lightning-30b-a3b", "nvidia/nemotron-3-ultra-550b-a55b"
TODAY = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def client(handlers, model="d/one,d/two"):
    asked = []

    def handler(request):
        m = json.loads(request.content)["model"]
        asked.append(m)
        return handlers[m]

    s = LlmSettings(api_key="k", base_url="https://example.test/v1", model=model)
    return LlmClient(s, transport=httpx.MockTransport(handler)), asked


def good():
    return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": 1}'}, "finish_reason": "stop"}]})


def test_models_override_respected_for_that_call_only():
    c, asked = client({"x/a": good(), "x/b": good(), "d/one": good(), "d/two": good()})
    assert c.complete_json("s", "u", models=("x/b", "x/a")) == {"a": 1}
    assert asked == ["x/b"] and c.last_model == "x/b"
    c.complete_json("s", "u")
    assert asked == ["x/b", "d/one"]  # default order untouched


def test_override_falls_back_in_override_order():
    c, asked = client({"x/a": httpx.Response(503, text="x"), "x/b": good()})
    c.complete_json("s", "u", models=("x/a", "x/b"))
    assert asked == ["x/a", "x/b"]


def test_override_truncated_only_falls_through_for_interactive_calls():
    trunc = httpx.Response(200, json={"choices": [{"message": {"content": "{"}, "finish_reason": "length"}]})
    c, asked = client({"x/a": trunc, "x/b": good()})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u", models=("x/a", "x/b"))
    assert ei.value.reason == "truncated" and asked == ["x/a"]
    asked.clear()
    assert c.complete_json("s", "u", read_timeout=30, models=("x/a", "x/b")) == {"a": 1}
    assert asked == ["x/a", "x/b"]


def test_default_interactive_order_is_strong_first(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_INTERACTIVE", raising=False)
    assert config.get_llm_settings().interactive_models == (BIG, FAST)
    assert interactive_models() == (BIG, FAST)
    assert config.parse_models(config.DEFAULT_LLM_MODEL)[0] == BIG  # background order unchanged


@pytest.mark.parametrize("value,expected", [
    (" a/x , b/y,a/x,, ", ("a/x", "b/y")),
    ("solo/m", ("solo/m",)),
    ("", (BIG, FAST)),
    (" , ,", (BIG, FAST)),
])
def test_env_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("LLM_MODEL_INTERACTIVE", value)
    assert config.get_llm_settings().interactive_models == expected


def test_env_does_not_change_background_models(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_INTERACTIVE", "a/x")
    monkeypatch.setenv("LLM_MODEL", "b/y")
    s = config.get_llm_settings()
    assert s.models == ("b/y",) and s.interactive_models == ("a/x",)


def test_ask_counselor_and_summary_pass_interactive_list(conn, monkeypatch):
    monkeypatch.setenv("LLM_MODEL_INTERACTIVE", "a/x,b/y")
    conn.execute("INSERT INTO entries(created_at, local_date, text) VALUES (?,?,?)",
                 ("2026-10-10T12:00:00+00:00", "2026-10-10", "walked along the river and felt calm"))
    conn.commit()
    llm = FakeLLM([{"answer": "x", "citations": [], "found": False}])
    ask.answer(llm, ask.prepare(conn, "river calm", TODAY))
    assert llm.calls[0]["models"] == ("a/x", "b/y")

    cid = counselor.start(conn, NOW)
    counselor.add_user_message(conn, cid, "river calm", NOW)
    llm = FakeLLM([{"reply": "What stays with you?", "cited_entry_ids": []}])
    counselor.reply(llm, counselor.prepare(conn, cid, TODAY))
    assert llm.calls[0]["models"] == ("a/x", "b/y")

    llm = FakeLLM([{"notes": []}])
    chat_memory.summarize(llm, [{"role": "user", "text": "hi"}])
    assert llm.calls[0]["models"] == ("a/x", "b/y")


def test_ask_prompt_trims_entries_to_1500(conn):
    assert prompts.ASK_ENTRY_CHARS == 1500
    text = "a" * 1499 + "b" + "c" * 400
    conn.execute("INSERT INTO entries(created_at, local_date, text) VALUES (?,?,?)",
                 ("2026-10-10T12:00:00+00:00", "2026-10-10", text))
    conn.commit()
    rows = conn.execute("SELECT id, local_date, text FROM entries").fetchall()
    out = prompts.ask_user("q", rows)
    assert "a" * 1499 + "b" in out
    assert "b" + "c" not in out


def test_escalation_model_default_and_env(monkeypatch):
    from app.llm import escalation_model
    monkeypatch.delenv("LLM_MODEL_ESCALATE", raising=False)
    assert escalation_model() == BIG
    monkeypatch.setenv("LLM_MODEL_ESCALATE", " a/x , b/y")
    assert escalation_model() == "a/x"
    monkeypatch.setenv("LLM_MODEL_ESCALATE", " , ")
    assert escalation_model() == BIG
