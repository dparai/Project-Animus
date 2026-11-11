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
DIMENSIONS = 768
BATCH = 32
MAX_CHARS = 6000
CHUNK_AT = 1500  # entries longer than this also get one vector per part
CHUNK_SIZE = 900
ADAPTIVE_MIN_ROWS = 10  # with this many scores the cut-off follows the journal's own spread
GOOGLE_HOST = "generativelanguage.googleapis.com"
TASKS = {"document": "RETRIEVAL_DOCUMENT", "query": "RETRIEVAL_QUERY"}
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
MIN_SCORE = 0.80  # floor used while the journal is too small for an adaptive cut-off
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

    def embed(self, texts: list[str], task: str = "document") -> list[list[float]]:
        """`task` is "document" (stored text) or "query" (a question). Google's own endpoint
        honours it; the OpenAI-compatible one has no such field and is used for other providers."""
        s = self._settings or config.get_llm_settings()
        if not s.api_key:
            raise LLMError("no API key configured", retryable=False, reason="no_key")
        native = GOOGLE_HOST in s.base_url
        if native:
            name = "models/" + model_name()
            url = f"https://{GOOGLE_HOST}/v1beta/{name}:batchEmbedContents"
            body = {"requests": [
                {"model": name, "content": {"parts": [{"text": t[:MAX_CHARS]}]},
                 "taskType": TASKS.get(task, TASKS["document"]), "outputDimensionality": DIMENSIONS}
                for t in texts]}
            headers = {"x-goog-api-key": s.api_key}
        else:
            url = s.base_url.rstrip("/") + "/embeddings"
            body = {"model": model_name(), "input": [t[:MAX_CHARS] for t in texts], "dimensions": DIMENSIONS}
            headers = {"Authorization": f"Bearer {s.api_key}"}
        try:
            with httpx.Client(transport=self._transport, timeout=TIMEOUT) as http:
                resp = http.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise LLMError("embedding request timed out", retryable=True, reason="timeout") from None
        except httpx.RequestError:
            raise LLMError("could not reach embeddings API", retryable=True, reason="unreachable") from None
        if resp.status_code >= 400:
            raise LLMError(f"embeddings API error (HTTP {resp.status_code})",
                           retryable=resp.status_code in (408, 429) or resp.status_code >= 500,
                           reason=f"http_{resp.status_code}")
        try:
            if native:
                vectors = [_normalise([float(x) for x in d["values"]]) for d in resp.json()["embeddings"]]
            else:
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


def _embed(embedder, texts: list[str], task: str) -> list[list[float]]:
    try:
        return embedder.embed(texts, task=task)
    except TypeError:  # an embedder without task support
        return embedder.embed(texts)


def entry_text(row) -> str:
    """What gets embedded for an entry: the day, mood and energy first, then the words."""
    head = []
    try:
        day = datetime.strptime(row["local_date"], "%Y-%m-%d")
        head.append(day.strftime("%A %d %B %Y"))
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    try:
        if row["mood"]:
            head.append(f"mood {row['mood']}")
        if row["energy_score"]:
            head.append(f"energy {row['energy_score']}/10")
    except (IndexError, KeyError):
        pass
    return (". ".join(head) + ".\n" if head else "") + row["text"]


def split_parts(text: str) -> list[str]:
    """Paragraph-sized parts of a long entry (empty list for a short one)."""
    if len(text) <= CHUNK_AT:
        return []
    parts, cur = [], ""
    for para in [p.strip() for p in text.replace("\r", "").split("\n") if p.strip()]:
        while len(para) > CHUNK_SIZE:  # one huge paragraph: cut at a sentence end when possible
            cut = max(para.rfind(". ", 0, CHUNK_SIZE), para.rfind("? ", 0, CHUNK_SIZE),
                      para.rfind("! ", 0, CHUNK_SIZE))
            cut = cut + 1 if cut > CHUNK_SIZE // 3 else CHUNK_SIZE
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:cut].strip())
            para = para[cut:].strip()
        if cur and len(cur) + len(para) + 1 > CHUNK_SIZE:
            parts.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        parts.append(cur)
    return parts if len(parts) > 1 else []


def _store_part(conn, kind: str, ref_id: int, part: int, vec: list[float]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO vectors(kind, ref_id, part, model, dim, vector, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (kind, ref_id, part, model_name(), len(vec), pack(vec), _now()),
    )


def fill_missing(conn: sqlite3.Connection, embedder, limit: int = FILL_PER_RUN) -> int:
    """Embed up to `limit` entries that have no current embedding (new, edited or not yet done).

    Long entries also get one vector per part. Newest first. Returns how many entries were
    stored. Raises LLMError so callers can decide."""
    rows = conn.execute(
        "SELECT e.* FROM entries e"
        " LEFT JOIN entry_embeddings v ON v.entry_id = e.id AND v.model = ? AND v.dim = ?"
        " WHERE v.entry_id IS NULL ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
        (model_name(), DIMENSIONS, limit),
    ).fetchall()
    stored = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        vectors = _embed(embedder, [entry_text(r) for r in chunk], "document")
        for r, vec in zip(chunk, vectors):
            conn.execute("DELETE FROM vectors WHERE kind = 'chunk' AND ref_id = ?", (r["id"],))
            parts = split_parts(r["text"])
            for j in range(0, len(parts), BATCH):
                for k, pv in enumerate(_embed(embedder, parts[j:j + BATCH], "document")):
                    _store_part(conn, "chunk", r["id"], j + k, pv)
            _store(conn, r["id"], vec)
            stored += 1
        conn.commit()
    return stored


def fill_missing_notes(conn: sqlite3.Connection, embedder, limit: int = FILL_PER_RUN) -> int:
    """Embed active memory notes that have no current vector. Returns how many were stored."""
    rows = conn.execute(
        "SELECT m.id, m.text FROM ai_memory m"
        " LEFT JOIN vectors v ON v.kind = 'memory' AND v.ref_id = m.id AND v.part = 0"
        "  AND v.model = ? AND v.dim = ?"
        " WHERE m.deleted_at IS NULL AND v.ref_id IS NULL ORDER BY m.id DESC LIMIT ?",
        (model_name(), DIMENSIONS, limit),
    ).fetchall()
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        for r, vec in zip(chunk, _embed(embedder, [r["text"] for r in chunk], "document")):
            _store_part(conn, "memory", r["id"], 0, vec)
        conn.commit()
    return len(rows)


def embed_query(embedder, text: str):
    """Vector for a question, or None when embedding is unavailable. Never raises."""
    if embedder is None or not (text or "").strip():
        return None
    try:
        return _embed(embedder, [text[:MAX_CHARS]], "query")[0]
    except LLMError as exc:
        log.info("query embedding failed reason=%s", exc.reason)
    except Exception as exc:
        log.warning("query embedding failed error=%s", type(exc).__name__)
    return None


def search_entries(conn: sqlite3.Connection, qvec, limit: int, date_from: str | None = None,
                   date_to: str | None = None) -> list[tuple[int, float]]:
    """Entries closest in meaning to a question vector: [(id, score)] best first. An entry's
    score is its best match among the whole-entry vector and its parts."""
    where, params = "", [model_name(), DIMENSIONS]
    if date_from and date_to:
        where = " AND e.local_date BETWEEN ? AND ?"
        params += [date_from, date_to]
    best: dict[int, float] = {}
    for sql in (
        "SELECT v.entry_id AS id, v.vector FROM entry_embeddings v JOIN entries e ON e.id = v.entry_id"
        " WHERE v.model = ? AND v.dim = ?" + where,
        "SELECT v.ref_id AS id, v.vector FROM vectors v JOIN entries e ON e.id = v.ref_id"
        " WHERE v.kind = 'chunk' AND v.model = ? AND v.dim = ?" + where,
    ):
        for r in conn.execute(sql, params):
            score = dot(qvec, unpack(r["vector"]))
            if score > best.get(r["id"], -1.0):
                best[r["id"]] = score
    return sorted(best.items(), key=lambda t: -t[1])[:limit]


def rank_notes(conn: sqlite3.Connection, qvec, ids: list[int]) -> list[int]:
    """The given memory note ids, most relevant to the question first (unembedded ones last)."""
    if not ids:
        return []
    scores = {}
    for r in conn.execute(
        "SELECT ref_id, vector FROM vectors WHERE kind = 'memory' AND part = 0 AND model = ? AND dim = ?",
        (model_name(), DIMENSIONS),
    ):
        scores[r["ref_id"]] = dot(qvec, unpack(r["vector"]))
    return sorted(ids, key=lambda i: -scores.get(i, -1.0))


def _cutoff(scores: list[float], floor: float) -> float:
    """Mean plus one standard deviation once there are enough scores; the fixed floor before."""
    if len(scores) < ADAPTIVE_MIN_ROWS:
        return floor
    mean = sum(scores) / len(scores)
    std = math.sqrt(sum((x - mean) ** 2 for x in scores) / len(scores))
    return mean + std


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
    every = []
    for r in conn.execute(
        "SELECT v.entry_id, v.vector FROM entry_embeddings v JOIN entries e ON e.id = v.entry_id"
        " WHERE v.model = ? AND v.dim = ? AND (e.created_at, e.id) < (?, ?)"
        " AND e.id NOT IN (SELECT to_entry_id FROM entry_links WHERE from_entry_id = ?)"
        " AND e.id NOT IN (SELECT from_entry_id FROM entry_links WHERE to_entry_id = ?)",
        (model_name(), DIMENSIONS, me["created_at"], entry_id, entry_id, entry_id),
    ):
        every.append((r["entry_id"], dot(mine, unpack(r["vector"]))))
    cut = _cutoff([s for _, s in every], min_score)
    scored = [t for t in every if t[1] >= cut]
    scored.sort(key=lambda t: -t[1])
    return scored[:limit]
