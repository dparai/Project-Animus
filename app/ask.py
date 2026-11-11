import logging
import re
import time
from dataclasses import dataclass
from datetime import date

from . import prompts, search
from .llm import LLMError, escalation_model, interactive_models
from .reflect import _int, _list, _match_quote, _model_name, _norm, _str

log = logging.getLogger("animus.ask")

QUESTION_MAX = 500
TOP_ENTRIES = 8
READ_TIMEOUT = 90.0
MAX_CALLS = 2  # first call plus one escalation (or plain retry with a single model)
ASK_TOKENS = 8192  # first call and retry; reasoning models spend much of it thinking
MAX_CITATIONS = 6
TIME_BUDGET = 60.0  # no escalation or retry once the first call has taken longer than this


def _clock() -> float:
    return time.monotonic()

NOT_FOUND = "The journal does not mention this."
_ERROR_TEXTS = {
    "no_key": "No API key is configured, so I cannot answer questions yet.",
    "timeout": "The model took too long to answer. Please try again in a moment.",
    "unreachable": "I could not reach the model just now. Please try again in a moment.",
    "http_429": "The model is busy right now (rate limit). Please try again in a minute.",
    "error": "The model could not answer this time. Please try again later.",
}


def error_text(reason: str) -> str:
    return _ERROR_TEXTS.get(reason, _ERROR_TEXTS["error"])


@dataclass(frozen=True)
class AskContext:
    question: str
    entries: tuple


@dataclass(frozen=True)
class Citation:
    entry_id: int
    date: str
    quote: str


@dataclass(frozen=True)
class AskResult:
    found: bool
    answer: str
    citations: tuple = ()
    error: str | None = None
    model: str | None = None


def clean_question(question) -> str:
    """The question as it will be searched and asked; ValueError when empty or too long."""
    question = _norm(question or "")
    if not question or len(question) > QUESTION_MAX:
        raise ValueError("question")
    return question


def prepare(conn, question: str, today: date, qvec=None) -> AskContext:
    question = clean_question(question)
    rows = search.find_entries_hybrid(conn, question, today, TOP_ENTRIES, qvec)
    entries = tuple(
        {"id": r["id"], "local_date": r["local_date"], "text": r["text"],
         "mood": r["mood"], "energy_score": r["energy_score"]}
        for r in rows
    )
    return AskContext(question=question, entries=entries)


def _verify(raw, entries) -> tuple[str, bool, tuple]:
    """Returns (answer text, model said found, verified citations). Never raises on odd shapes."""
    raw = raw if isinstance(raw, dict) else {}
    by_id = {e["id"]: e for e in entries}
    said_found = raw.get("found") is not False
    text = _norm(prompts.unescape(_str(raw.get("answer"))))  # display text only
    seen: set = set()
    cites: list[Citation] = []
    for item in _list(raw.get("citations")):
        if not isinstance(item, dict):
            continue
        entry = by_id.get(_int(item.get("entry_id")))
        if entry is None:
            continue
        quote = _match_quote(_str(item.get("quote")), entry["text"])
        if quote is None or (entry["id"], quote) in seen:
            continue
        seen.add((entry["id"], quote))
        cites.append(Citation(entry_id=entry["id"], date=entry["local_date"], quote=quote))
    return text, said_found, tuple(cites[:MAX_CITATIONS])


_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december")
_MONTH_NUM = {}
for _i, _name in enumerate(_MONTHS, start=1):
    _MONTH_NUM[_name] = _i
    _MONTH_NUM[_name[:3]] = _i
_MONTH_NUM["sept"] = 9
_FACT = re.compile(r"\d+|\b(?:" + "|".join(sorted(_MONTH_NUM, key=len, reverse=True)) + r")\b", re.IGNORECASE)


def _tokens(text: str) -> set:
    """Digit sequences (zeros trimmed) and month names (as month numbers) found in text."""
    out = set()
    for m in _FACT.finditer(text):
        tok = m.group(0)
        if tok.isdigit():
            out.add(tok.lstrip("0") or "0")
        elif tok.lower() == "may" and tok != "May":
            continue  # lower-case "may" is an ordinary word
        else:
            out.add(("m", _MONTH_NUM[tok.lower()]))
    return out


def _supported(text: str, cites: tuple, entries) -> bool:
    """Every date or number in the answer must appear in (or be the local_date of) a cited entry."""
    wanted = _tokens(text)
    if not wanted:
        return True
    by_id = {e["id"]: e for e in entries}
    have = set()
    for c in cites:
        entry = by_id[c.entry_id]
        have |= _tokens(entry["text"])
        d = str(entry["local_date"])
        have |= {p.lstrip("0") or "0" for p in d.split("-")}
        parts = d.split("-")
        if len(parts) == 3 and parts[1].isdigit() and 1 <= int(parts[1]) <= 12:
            have.add(("m", int(parts[1])))
    return wanted <= have


def _call(llm, system, prompt, models, thinking=False):
    return llm.complete_json(system, prompt, max_tokens=ASK_TOKENS, read_timeout=READ_TIMEOUT, models=models,
                            thinking=thinking)


def _error_result(exc) -> AskResult:
    if isinstance(exc, LLMError):  # never retried, "truncated" on all models included
        log.warning("ask failed reason=%s", exc.reason)
        return AskResult(found=False, answer=error_text(exc.reason), error=exc.reason)
    log.warning("ask failed reason=error error=%s", type(exc).__name__)  # class name only, never the message
    return AskResult(found=False, answer=error_text("error"), error="error")


def answer(llm, ctx: AskContext, progress=None) -> AskResult:
    """First call uses the interactive model order. With no verified answer, one more call may follow:
    forced onto the escalation (strongest) model, even if that model made the first attempt. The first call
    runs with thinking off for speed, the second always with full thinking; with a single interactive
    model it is a plain retry (also with thinking)."""
    def note(stage: str) -> None:  # optional live status for the page; never allowed to break the answer
        if progress is not None:
            try:
                progress(stage)
            except Exception:
                pass

    if not ctx.entries:
        return AskResult(found=False, answer=NOT_FOUND)
    system = prompts.ask_system()
    user = prompts.ask_user(ctx.question, ctx.entries)
    models = interactive_models()
    started = _clock()
    first_model = None
    big = escalation_model()
    for attempt in range(MAX_CALLS):
        escalate = attempt > 0 and len(models) > 1
        if attempt and _clock() - started > TIME_BUDGET:
            log.info("ask second call skipped, time budget used")
            break
        prompt = user if attempt == 0 or not dropped else user + "\n\n" + prompts.ask_retry_note()
        note("retry" if attempt else "asking")
        try:
            raw = _call(llm, system, prompt, (big,) if escalate else models, thinking=attempt > 0)
        except Exception as exc:
            if escalate:
                if isinstance(exc, LLMError):  # the real answer attempt failed: say so, not "not mentioned"
                    return _error_result(exc)
                log.warning("ask escalation failed error=%s", type(exc).__name__)
                break
            return _error_result(exc)
        note("checking")
        text, said_found, cites = _verify(raw, ctx.entries)
        if text and cites and said_found and _supported(text, cites, ctx.entries):
            return AskResult(found=True, answer=text, citations=cites, model=_model_name(llm))
        if attempt == 0:
            first_model = _model_name(llm)
            dropped = said_found
            if not said_found and len(models) < 2:
                break  # single model said not found: nothing to escalate to
        log.info("ask no verified answer attempt=%d said_found=%s", attempt + 1, said_found)
    return AskResult(found=False, answer=NOT_FOUND, model=first_model or _model_name(llm))
