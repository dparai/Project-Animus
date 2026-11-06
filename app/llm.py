import json
import logging
import re

import httpx

from app.config import LlmSettings, get_llm_settings, parse_models

log = logging.getLogger("animus.llm")

# Reasoning models answer non-streamed, so the read timeout must cover the whole generation.
TIMEOUT = httpx.Timeout(connect=10.0, read=240.0, write=30.0, pool=10.0)
TEMPERATURE = 0.3

_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL)
TRUNCATED_MESSAGE = "model output truncated (max_tokens too small or reasoning consumed budget)"
NO_JSON_MESSAGE = "model returned no JSON object"


# Fixed reason tokens (stored in reflection_jobs.last_error; never response bodies or key material):
# no_key timeout unreachable http_401 http_403 http_404 http_408 http_429 http_5xx http_4xx
# no_json bad_shape truncated error
class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool, reason: str = "error", status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason
        self.status = status


def extract_json_object(text: str) -> dict:
    """Parse the JSON object that starts at the first "{" after reasoning blocks are removed."""
    text = text or ""
    # Templates that pre-inject "<think>" leave only "reasoning</think>answer".
    close = text.rfind("</think>")
    if close != -1:
        text = text[close + len("</think>"):]
    text = _THINK_RE.sub("", text)
    pos = text.find("{")
    if pos == -1 or _starts_with_array(text):
        raise LLMError(NO_JSON_MESSAGE, retryable=False, reason="no_json")
    try:
        value, _ = json.JSONDecoder().raw_decode(text, pos)
    except ValueError:
        raise LLMError(NO_JSON_MESSAGE, retryable=False, reason="no_json") from None
    if not isinstance(value, dict):
        raise LLMError(NO_JSON_MESSAGE, retryable=False, reason="no_json")
    return value


def _starts_with_array(text: str) -> bool:
    """True when the answer itself (after an optional opening code fence line) is a top-level array."""
    body = text.lstrip()
    if body.startswith("```"):
        _, _, body = body.partition(chr(10))
    return body.lstrip().startswith("[")


# Failures that move on to the next configured model. Key problems (no_key, 401, 403), other 4xx and
# truncation (the caller retries with a larger budget) are raised immediately instead.
_FALLBACK_REASONS = frozenset({
    "http_429", "http_5xx", "http_408", "timeout", "unreachable", "http_404", "no_json", "bad_shape",
})
# Interactive calls (those passing read_timeout) cannot retry with a bigger budget, so a model that
# spent its tokens reasoning hands over to the next model instead.
_INTERACTIVE_FALLBACK_REASONS = _FALLBACK_REASONS | {"truncated"}


def escalation_model() -> str:
    """The strongest interactive model, the only one Ask escalates to (LLM_MODEL_ESCALATE)."""
    return get_llm_settings().escalation_model


def interactive_models() -> tuple[str, ...]:
    """Model order for calls a person is waiting on (LLM_MODEL_INTERACTIVE; strong model first)."""
    return get_llm_settings().interactive_models


class LlmClient:
    def __init__(self, settings: LlmSettings, *, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._transport = transport
        self.last_model: str | None = None  # the model that answered the latest successful call
        self._no_thinking_kwarg: set[str] = set()  # models that rejected chat_template_kwargs

    def __repr__(self) -> str:
        return f"LlmClient(base_url={self._settings.base_url!r}, model={self._settings.model!r})"

    def complete_json(self, system: str, user: str, *, max_tokens: int = 4096,
                      read_timeout: float | None = None,
                      models: tuple[str, ...] | None = None, thinking: bool = True) -> dict:
        """models overrides the configured model order for this call only.

        thinking=False asks the model to skip its reasoning phase (chat_template_kwargs enable_thinking)."""
        s = self._settings
        self.last_model = None
        if not s.api_key:
            raise LLMError("no API key configured", retryable=False, reason="no_key")
        models = tuple(models) if models else (parse_models(s.model) or (s.model,))
        any_retryable = False
        fallback = _FALLBACK_REASONS if read_timeout is None else _INTERACTIVE_FALLBACK_REASONS
        for i, model in enumerate(models):
            try:
                result = self._attempt(model, system, user, max_tokens, read_timeout, thinking)
            except LLMError as exc:
                any_retryable = any_retryable or exc.retryable
                if exc.reason not in fallback or i == len(models) - 1:
                    exc.retryable = exc.retryable or (any_retryable and exc.reason in fallback)
                    raise
                log.info("llm fallback from=%s to=%s reason=%s", model, models[i + 1], exc.reason)
                continue
            self.last_model = model
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    def _attempt(self, model: str, system: str, user: str, max_tokens: int,
                 read_timeout: float | None, thinking: bool) -> dict:
        no_think = not thinking and model not in self._no_thinking_kwarg
        try:
            return self._complete_one(model, system, user, max_tokens, read_timeout, no_think)
        except LLMError as exc:
            if not (no_think and exc.reason == "http_4xx" and exc.status in (400, 422)):
                raise
        # The model rejected the unknown field: remember it and retry once without it.
        self._no_thinking_kwarg.add(model)
        return self._complete_one(model, system, user, max_tokens, read_timeout, False)

    def _complete_one(self, model: str, system: str, user: str, max_tokens: int,
                      read_timeout: float | None = None, no_think: bool = False) -> dict:
        s = self._settings
        timeout = TIMEOUT if read_timeout is None else httpx.Timeout(
            connect=TIMEOUT.connect, read=read_timeout, write=TIMEOUT.write, pool=TIMEOUT.pool)
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": TEMPERATURE,
            "max_tokens": max_tokens,
        }
        if no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Authorization": f"Bearer {s.api_key}"}
        url = s.base_url.rstrip("/") + "/chat/completions"
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as http:
                resp = http.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMError("request to LLM API timed out", retryable=True, reason="timeout") from None
        except httpx.TransportError:
            raise LLMError("could not reach LLM API", retryable=True, reason="unreachable") from None
        except httpx.RequestError:
            raise LLMError("unreadable response from LLM API", retryable=False, reason="bad_shape") from None

        status = resp.status_code
        if status == 408 or status == 429 or status >= 500:
            reason = "http_5xx" if status >= 500 else f"http_{status}"
            raise LLMError(f"LLM API temporary error (HTTP {status})", retryable=True, reason=reason)
        if status >= 400:
            reason = f"http_{status}" if status in (401, 403, 404) else "http_4xx"
            raise LLMError(f"LLM API request rejected (HTTP {status})", retryable=False, reason=reason,
                           status=status)
        try:
            choice = resp.json()["choices"][0]
            content = choice["message"].get("content")
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("unexpected response shape from LLM API", retryable=False, reason="bad_shape") from None
        truncated = finish == "length"
        if not isinstance(content, str):
            if content is None or truncated:
                content = ""
            else:
                raise LLMError("unexpected response shape from LLM API", retryable=False, reason="bad_shape")
        try:
            return extract_json_object(content)
        except LLMError:
            if truncated:
                raise LLMError(TRUNCATED_MESSAGE, retryable=False, reason="truncated") from None
            raise
