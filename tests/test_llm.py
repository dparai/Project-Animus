import json
import logging

import httpx
import pytest

from app.config import NvidiaSettings
from app.llm import LLMError, NvidiaClient

KEY = "nvapi" + "-SECRET-KEY-123"


def settings(key=KEY):
    return NvidiaSettings(api_key=key, base_url="https://example.test/v1", model="m")


def chat(content):
    return {"choices": [{"message": {"content": content}}]}


def client(handler, key=KEY):
    return NvidiaClient(settings(key), transport=httpx.MockTransport(handler))


def test_returns_parsed_json():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=chat('{"a": 1}'))

    assert client(handler).complete_json("sys", "usr", max_tokens=50) == {"a": 1}
    assert seen["url"] == "https://example.test/v1/chat/completions"
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["body"]["model"] == "m"
    assert seen["body"]["max_tokens"] == 50
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "usr"}
    assert seen["body"]["temperature"] <= 0.3


def test_strips_code_fences_and_prose():
    text = 'Sure! Here you go:\n```json\n{"a": {"b": [1, 2]}}\n```\nHope that helps {not json}'
    c = client(lambda r: httpx.Response(200, json=chat(text)))
    assert c.complete_json("s", "u") == {"a": {"b": [1, 2]}}


@pytest.mark.parametrize("status", [429, 503])
def test_429_and_503_are_retryable(status):
    c = client(lambda r: httpx.Response(status, text="busy"))
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert ei.value.retryable is True


def test_401_not_retryable():
    c = client(lambda r: httpx.Response(401, text="nope"))
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert ei.value.retryable is False


def test_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError) as ei:
        client(handler).complete_json("s", "u")
    assert ei.value.retryable is True


def test_connection_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(LLMError) as ei:
        client(handler).complete_json("s", "u")
    assert ei.value.retryable is True


def test_no_key_raises_not_retryable_without_network_call():
    def handler(request):
        raise AssertionError("network call made")

    with pytest.raises(LLMError, match="no API key configured") as ei:
        client(handler, key=None).complete_json("s", "u")
    assert ei.value.retryable is False


def test_garbage_content_not_retryable():
    c = client(lambda r: httpx.Response(200, json=chat("I cannot do that.")))
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert ei.value.retryable is False


def test_api_key_never_in_error_message_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    cases = [
        httpx.Response(401, text=f"bad key {KEY} and prompt usr"),
        httpx.Response(503, text=KEY),
        httpx.Response(200, json=chat(f"garbage {KEY}")),
    ]
    for resp in cases:
        c = client(lambda r, resp=resp: resp)
        with pytest.raises(LLMError) as ei:
            c.complete_json("s", "usr")
        assert KEY not in str(ei.value)
        assert KEY not in repr(ei.value)
        assert "prompt usr" not in str(ei.value)
        assert KEY not in repr(c)
    assert KEY not in caplog.text


def chat_fr(content, finish):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def call(resp_json):
    c = client(lambda r: httpx.Response(200, json=resp_json))
    return c.complete_json("s", "u")


def test_finish_reason_length_gives_truncated_error():
    with pytest.raises(LLMError) as ei:
        call(chat_fr('{"wins":[{"id":"w1","qu', "length"))
    assert ei.value.reason == "truncated"
    assert ei.value.retryable is False
    assert "truncated" in str(ei.value)


def test_empty_or_null_content_with_length_is_truncated():
    for content in (None, ""):
        with pytest.raises(LLMError) as ei:
            call(chat_fr(content, "length"))
        assert ei.value.reason == "truncated"


def test_think_block_with_braces_ignored():
    text = '<think>maybe {"x": 1} or {"y": 2}</think>\n```json\n{"a": 1}\n```'
    assert call(chat_fr(text, "stop")) == {"a": 1}


def test_unclosed_think_does_not_leak_braces():
    with pytest.raises(LLMError) as ei:
        call(chat_fr('<think>let me try {"x": 1} hmm', "length"))
    assert ei.value.reason == "truncated"
    with pytest.raises(LLMError) as ei:
        call(chat_fr('<think>let me try {"x": 1} hmm', "stop"))
    assert ei.value.reason == "no_json"


def test_cut_off_outer_object_raises_not_inner_dict():
    with pytest.raises(LLMError):
        call(chat_fr('{"wins":[{"id":"w1","quote":"q"},{"id":"w2","qu', "stop"))


def test_top_level_array_raises():
    with pytest.raises(LLMError) as ei:
        call(chat_fr('[{"id": "w1"}]', "stop"))
    assert ei.value.retryable is False


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{}]}, {"choices": [None]}, {"choices": ["x"]}])
def test_missing_or_empty_choices_raises_clean_error(body):
    with pytest.raises(LLMError) as ei:
        call(body)
    assert ei.value.retryable is False


def test_content_as_list_of_parts_raises_clean_error():
    with pytest.raises(LLMError) as ei:
        call(chat_fr([{"type": "text", "text": '{"a": 1}'}], "stop"))
    assert ei.value.retryable is False


def test_decoding_error_maps_to_non_retryable():
    def handler(request):
        raise httpx.DecodingError("bad gzip", request=request)

    with pytest.raises(LLMError) as ei:
        client(handler).complete_json("s", "u")
    assert ei.value.retryable is False


# --- F1: timeouts ---
def test_timeout_constant_and_client_construction(monkeypatch):
    from app import llm

    assert llm.TIMEOUT == httpx.Timeout(connect=10.0, read=240.0, write=30.0, pool=10.0)
    seen = {}
    real = httpx.Client

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm.httpx, "Client", spy)
    client(lambda r: httpx.Response(200, json=chat('{"a": 1}'))).complete_json("s", "u")
    assert seen["timeout"] == httpx.Timeout(connect=10.0, read=240.0, write=30.0, pool=10.0)


# --- F2: JSON extraction ---
def test_orphan_close_think_is_dropped():
    text = 'reasoning with {braces} </think>{"a":1}'
    assert call(chat_fr(text, "stop")) == {"a": 1}


def test_last_close_think_wins():
    text = 'a {x} </think> b {y} </think>{"a":2}'
    assert call(chat_fr(text, "stop")) == {"a": 2}


def test_bracket_in_prose_before_object_parses():
    assert call(chat_fr('see [1] {"a": 1}', "stop")) == {"a": 1}


def test_leading_array_still_raises():
    with pytest.raises(LLMError) as ei:
        call(chat_fr('  [{"id": "w1"}]', "stop"))
    assert ei.value.reason == "no_json"


@pytest.mark.parametrize("fence", ["```", "```json"])
def test_fenced_array_raises(fence):
    with pytest.raises(LLMError) as ei:
        call(chat_fr(fence + "\n" + '[{"id": "w1"}]' + "\n```", "stop"))
    assert ei.value.reason == "no_json"


def test_parses_only_at_first_brace():
    with pytest.raises(LLMError):
        call(chat_fr('{"a": 1,, "b": {"c": 2}}', "stop"))


# --- F3: fixed reason codes ---
@pytest.mark.parametrize("status,reason,retryable", [
    (401, "http_401", False), (403, "http_403", False), (404, "http_404", False),
    (408, "http_408", True), (429, "http_429", True), (500, "http_5xx", True),
    (503, "http_5xx", True), (400, "http_4xx", False), (422, "http_4xx", False),
])
def test_http_status_reason_codes(status, reason, retryable):
    c = client(lambda r: httpx.Response(status, text=f"body {KEY}"))
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert ei.value.reason == reason
    assert ei.value.retryable is retryable
    assert KEY not in str(ei.value)


def test_reason_timeout_unreachable_no_key_no_json_bad_shape():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def down(request):
        raise httpx.ConnectError("down", request=request)

    def undecodable(request):
        raise httpx.DecodingError("bad", request=request)

    for handler, reason, retryable in ((timeout, "timeout", True), (down, "unreachable", True),
                                       (undecodable, "bad_shape", False)):
        with pytest.raises(LLMError) as ei:
            client(handler).complete_json("s", "u")
        assert (ei.value.reason, ei.value.retryable) == (reason, retryable)
    with pytest.raises(LLMError, match="^no API key configured$") as ei:
        client(lambda r: None, key=None).complete_json("s", "u")
    assert ei.value.reason == "no_key"
    with pytest.raises(LLMError) as ei:
        call(chat_fr("I cannot do that.", "stop"))
    assert ei.value.reason == "no_json"
    for body in ({}, {"choices": []}, chat_fr([{"type": "text"}], "stop")):
        with pytest.raises(LLMError) as ei:
            call(body)
        assert ei.value.reason == "bad_shape"


# ---- model fallback ----
from app import config  # noqa: E402

M1, M2 = "vendor/first", "vendor/second"
SENTINEL = "SENTINEL-ENTRY-TEXT"


def multi_settings(models=f"{M1},{M2}", key=KEY):
    return NvidiaSettings(api_key=key, base_url="https://example.test/v1", model=models)


def run_models(responses, models=f"{M1},{M2}", key=KEY):
    """responses: dict model -> httpx.Response or Exception. Returns (client, requested model list)."""
    asked = []

    def handler(request):
        model = json.loads(request.content)["model"]
        asked.append(model)
        r = responses[model]
        if isinstance(r, Exception):
            raise r
        return r

    return NvidiaClient(multi_settings(models, key), transport=httpx.MockTransport(handler)), asked


def ok(content='{"a": 2}'):
    return httpx.Response(200, json=chat(content))


def test_single_model_behaviour_unchanged():
    c, asked = run_models({M1: httpx.Response(503, text="x")}, models=M1)
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert asked == [M1]
    assert (ei.value.reason, ei.value.retryable) == ("http_5xx", True)
    c, asked = run_models({M1: ok()}, models=M1)
    assert c.complete_json("s", "u") == {"a": 2}
    assert c.last_model == M1


def test_models_parsed_from_comma_list_with_whitespace_and_duplicates():
    assert config.parse_models(" a/b , c/d,,a/b ,  ,e") == ("a/b", "c/d", "e")
    assert config.parse_models("solo/model") == ("solo/model",)
    assert config.parse_models("") == ()
    s = multi_settings(" a/b , c/d,a/b")
    assert s.models == ("a/b", "c/d")
    assert s.model == " a/b , c/d,a/b"
    assert KEY not in repr(s)
    assert config.parse_models(config.DEFAULT_NVIDIA_MODEL) == (
        "nvidia/nemotron-3-ultra-550b-a55b", "nvidia/nemotron-3.5-lightning-30b-a3b")


@pytest.mark.parametrize("first", [
    httpx.Response(429, text="x"),
    httpx.Response(503, text="x"),
    httpx.ReadTimeout("slow"),
    httpx.Response(404, text="x"),
    httpx.Response(408, text="x"),
    httpx.ConnectError("down"),
    ok("no json here"),
    httpx.Response(200, json={"choices": []}),
], ids=["429", "503", "timeout", "404", "408", "unreachable", "no_json", "bad_shape"])
def test_falls_back_on_retryable_or_model_specific_failures(first):
    c, asked = run_models({M1: first, M2: ok()})
    assert c.last_model is None
    assert c.complete_json("s", "u") == {"a": 2}
    assert asked == [M1, M2]
    assert c.last_model == M2


@pytest.mark.parametrize("status", [401, 403])
def test_no_fallback_on_401_or_403(status):
    c, asked = run_models({M1: httpx.Response(status, text="x"), M2: ok()})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert asked == [M1]
    assert ei.value.reason == f"http_{status}"


def test_no_fallback_on_no_key_other_4xx_and_truncated():
    c, asked = run_models({M1: ok(), M2: ok()}, key=None)
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert asked == [] and ei.value.reason == "no_key"
    c, asked = run_models({M1: httpx.Response(400, text="x"), M2: ok()})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert asked == [M1] and ei.value.reason == "http_4xx"


def test_no_fallback_on_truncated():
    cut = httpx.Response(200, json=chat_fr('{"wins":[{"id"', "length"))
    c, asked = run_models({M1: cut, M2: ok()})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert asked == [M1]
    assert ei.value.reason == "truncated"


def test_all_models_fail_raises_last_error_and_retryable_if_any_retryable():
    c, asked = run_models({M1: httpx.Response(429, text="x"), M2: ok("no json")})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert asked == [M1, M2]
    assert ei.value.reason == "no_json"      # last error unchanged
    assert ei.value.retryable is True        # an earlier model failed retryably
    c, asked = run_models({M1: ok("no json"), M2: ok("still none")})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert (ei.value.reason, ei.value.retryable) == ("no_json", False)
    assert c.last_model is None


def test_fallback_logs_models_and_reason_only():
    records = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record)

    lg = logging.getLogger("animus.llm")
    h, old = H(), lg.level
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        c, _ = run_models({M1: httpx.Response(429, text=f"{SENTINEL} {KEY}"), M2: ok()})
        c.complete_json("s", SENTINEL)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    lines = [r.getMessage() for r in records]
    assert lines == [f"llm fallback from={M1} to={M2} reason=http_429"]
    assert all(r.levelno == logging.INFO for r in records)
    assert SENTINEL not in lines[0] and KEY not in lines[0]


def test_last_model_none_before_success_and_set_after():
    c, _ = run_models({M1: ok(), M2: ok()})
    assert c.last_model is None
    c.complete_json("s", "u")
    assert c.last_model == M1


def test_complete_json_read_timeout_override(monkeypatch):
    from app import llm

    seen = []
    real = httpx.Client

    def spy(*args, **kwargs):
        seen.append(kwargs["timeout"])
        return real(*args, **kwargs)

    monkeypatch.setattr(llm.httpx, "Client", spy)
    c = client(lambda r: httpx.Response(200, json=chat('{"a": 1}')))
    c.complete_json("s", "u", read_timeout=90)
    c.complete_json("s", "u")
    assert seen[0] == httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
    assert seen[1] == llm.TIMEOUT


def test_interactive_calls_fall_back_on_truncated_reflections_do_not():
    cut = httpx.Response(200, json=chat_fr('{"reply": "thinking', "length"))
    c, asked = run_models({M1: cut, M2: ok()})
    assert c.complete_json("s", "u", read_timeout=90) == {"a": 2}
    assert asked == [M1, M2] and c.last_model == M2
    c, asked = run_models({M1: cut, M2: ok()})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u")
    assert asked == [M1] and ei.value.reason == "truncated"
    c, asked = run_models({M1: cut, M2: cut})
    with pytest.raises(LLMError) as ei:
        c.complete_json("s", "u", read_timeout=90)
    assert asked == [M1, M2] and ei.value.reason == "truncated"


def _body_log(responses):
    log = []

    def handler(request):
        body = json.loads(request.content)
        log.append(body)
        return responses(body, len(log))
    return log, handler


def test_thinking_kwarg_only_when_disabled():
    log, h = _body_log(lambda b, n: httpx.Response(200, json=chat('{"a": 1}')))
    c = client(h)
    c.complete_json("s", "u")
    c.complete_json("s", "u", thinking=True)
    c.complete_json("s", "u", thinking=False)
    assert "chat_template_kwargs" not in log[0] and "chat_template_kwargs" not in log[1]
    assert log[2]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("status", [400, 422])
def test_rejected_kwarg_retries_once_and_is_remembered(status):
    def responses(b, n):
        if "chat_template_kwargs" in b:
            return httpx.Response(status, json={"error": "unknown field"})
        return httpx.Response(200, json=chat('{"a": 1}'))
    log, h = _body_log(responses)
    c = client(h)
    assert c.complete_json("s", "u", thinking=False) == {"a": 1}
    assert [("chat_template_kwargs" in b) for b in log] == [True, False]
    assert c.complete_json("s", "u", thinking=False) == {"a": 1}
    assert [("chat_template_kwargs" in b) for b in log] == [True, False, False]


def test_rejected_kwarg_is_remembered_per_model():
    def responses(b, n):
        if "chat_template_kwargs" in b and b["model"] == "bad":
            return httpx.Response(400, json={})
        return httpx.Response(200, json=chat('{"a": 1}'))
    log, h = _body_log(responses)
    c = client(h)
    c.complete_json("s", "u", thinking=False, models=("bad",))
    c.complete_json("s", "u", thinking=False, models=("good",))
    assert "chat_template_kwargs" in log[2] and log[2]["model"] == "good"


def test_retry_without_kwarg_failing_raises_after_two_calls():
    log, h = _body_log(lambda b, n: httpx.Response(400, json={}))
    with pytest.raises(LLMError) as ei:
        client(h).complete_json("s", "u", thinking=False, models=("a", "b"))
    assert ei.value.reason == "http_4xx"
    assert [("chat_template_kwargs" in b) for b in log] == [True, False]


def test_other_errors_do_not_retry_without_kwarg():
    log, h = _body_log(lambda b, n: httpx.Response(401, json={}))
    with pytest.raises(LLMError) as ei:
        client(h).complete_json("s", "u", thinking=False)
    assert ei.value.reason == "http_401" and len(log) == 1
    log, h = _body_log(lambda b, n: httpx.Response(413, json={}))
    with pytest.raises(LLMError) as ei:
        client(h).complete_json("s", "u", thinking=False)
    assert ei.value.reason == "http_4xx" and len(log) == 1


def test_default_thinking_400_not_retried():
    log, h = _body_log(lambda b, n: httpx.Response(400, json={}))
    with pytest.raises(LLMError) as ei:
        client(h).complete_json("s", "u")
    assert ei.value.reason == "http_4xx" and len(log) == 1
