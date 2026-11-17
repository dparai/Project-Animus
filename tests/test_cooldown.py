import sqlite3

from app import extract
from app.llm import LLMError


class Busy:
    calls = 0

    def complete_json(self, *a, **k):
        Busy.calls += 1
        raise LLMError("busy", retryable=True, reason="http_429")


def test_rate_limit_pauses_the_facts_pass(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE entries(id INTEGER PRIMARY KEY, text TEXT, created_at TEXT);"
        "CREATE TABLE entry_facts(entry_id INTEGER PRIMARY KEY, data TEXT);"
        "INSERT INTO entries VALUES (1, 'a', '2026-01-01'), (2, 'b', '2026-01-02');")
    monkeypatch.setattr(extract, "_cooldown_until", 0.0)
    monkeypatch.setattr(extract, "extract_entry", lambda c, l, i: Busy().complete_json())
    Busy.calls = 0
    assert extract.fill_missing(conn, object()) == 0
    assert Busy.calls == 1  # stopped at the first failure
    assert extract.fill_missing(conn, object()) == 0
    assert Busy.calls == 1  # resting: no new call
