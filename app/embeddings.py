"""Entry embeddings: meaning-based neighbours for link suggestions.

Vectors come from the same provider as the chat model (OpenAI-compatible /embeddings), are
normalised, and stored as float32 blobs in SQLite. Similarity is a plain dot product in Python,
which is instant at journal scale. Everything here degrades gracefully: with no key or on any
error the callers fall back to keyword search only.

Journal safety: logs hold only ids, counts and reason codes. Never entry text or vectors.
"""
import logging
import math
import os
import sqlite3
from array import array
from datetime import datetime, timezone

import httpx

from app import config
from app.llm import LLMError

log = logging.getLogger("animus.embeddings")

DEFAULT_MODEL = "gemini-embedding-001"
DIMENSIONS = 256
BATCH = 32
MAX_CHARS = 2000
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
MIN_SCORE = 0.80  # journal text scores high on average (median 0.78), so only the top tail counts
FILL_PER_RUN = 50


def model_name() -> str:
    return os.environ.get("LLM_EMBED_MODEL", "").strip() or DEFAULT_MODEL


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def pack(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob: bytes) -> array:
    out = array("f")
    out.frombytes(blob)
    return out


def dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    """Calls the provider's embeddings endpoint. Tests substitute any object with `embed`."""

    def __init__(self, settings: config.LlmSettings | None = None, *, transport: httpx.BaseTransport | None = None):
        self._settings = settings
        self._transport = transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        s = self._settings or config.get_llm_settings()
        if not s.api_key:
            raise LLMError("no API key configured", retryable=False, reason="no_key")
        body = {"model": model_name(), "input": [t[:MAX_CHARS] for t in texts], "dimensions": DIMENSIONS}
        headers = {"Authorization": f"Bearer {s.api_key}"}
        try:
            with httpx.Client(transport=self._transport, timeout=TIMEOUT) as http:
                resp = http.post(s.base_url.rstrip("/") + "/embeddings", json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMError("embedding request timed out", retryable=True, reason="timeout") from None
        except httpx.RequestError:
            raise LLMError("could not reach embeddings API", retryable=True, reason="unreachable") from None
        if resp.status_code >= 400:
            raise LLMError(f"embeddings API error (HTTP {resp.status_code})",
                           retryable=resp.status_code in (408, 429) or resp.status_code >= 500,
                           reason=f"http_{resp.status_code}")
        try:
            data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [_normalise([float(x) for x in d["embedding"]]) for d in data]
        except (ValueError, KeyError, TypeError):
            raise LLMError("unexpected embeddings response", retryable=False, reason="bad_shape") from None
        if len(vectors) != len(texts) or any(len(v) != len(vectors[0]) for v in vectors):
            raise LLMError("unexpected embeddings response", retryable=False, reason="bad_shape")
        return vectors


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store(conn: sqlite3.Connection, entry_id: int, vec: list[float]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO entry_embeddings(entry_id, model, dim, vector, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (entry_id, model_name(), len(vec), pack(vec), _now()),
    )


def fill_missing(conn: sqlite3.Connection, embedder, limit: int = FILL_PER_RUN) -> int:
    """Embed up to `limit` entries that have no current embedding (new, edited or not yet done).

    Newest first. Returns how many were stored. Raises LLMError so callers can decide."""
    rows = conn.execute(
        "SELECT e.id, e.text FROM entries e"
        " LEFT JOIN entry_embeddings v ON v.entry_id = e.id AND v.model = ? AND v.dim = ?"
        " WHERE v.entry_id IS NULL ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
        (model_name(), DIMENSIONS, limit),
    ).fetchall()
    stored = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        vectors = embedder.embed([r["text"] for r in chunk])
        for r, vec in zip(chunk, vectors):
            _store(conn, r["id"], vec)
            stored += 1
        conn.commit()
    return stored


def semantic_neighbours(conn: sqlite3.Connection, entry_id: int, limit: int,
                        min_score: float = MIN_SCORE) -> list[tuple[int, float]]:
    """Older, not-yet-linked entries closest in meaning to this one: [(id, cosine)] best first."""
    me = conn.execute(
        "SELECT v.vector, e.created_at FROM entry_embeddings v JOIN entries e ON e.id = v.entry_id"
        " WHERE v.entry_id = ? AND v.model = ? AND v.dim = ?",
        (entry_id, model_name(), DIMENSIONS),
    ).fetchone()
    if me is None:
        return []
    mine = unpack(me["vector"])
    scored = []
    for r in conn.execute(
        "SELECT v.entry_id, v.vector FROM entry_embeddings v JOIN entries e ON e.id = v.entry_id"
        " WHERE v.model = ? AND v.dim = ? AND (e.created_at, e.id) < (?, ?)"
        " AND e.id NOT IN (SELECT to_entry_id FROM entry_links WHERE from_entry_id = ?)"
        " AND e.id NOT IN (SELECT from_entry_id FROM entry_links WHERE to_entry_id = ?)",
        (model_name(), DIMENSIONS, me["created_at"], entry_id, entry_id, entry_id),
    ):
        score = dot(mine, unpack(r["vector"]))
        if score >= min_score:
            scored.append((r["entry_id"], score))
    scored.sort(key=lambda t: -t[1])
    return scored[:limit]
