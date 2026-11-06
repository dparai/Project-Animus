from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import embeddings, entries, links
from app.config import LlmSettings
from app.llm import LLMError
from tests.fake_llm import FakeLLM

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def add(conn, text, days):
    return entries.create_entry(conn, text=text, mood=None, energy=None, now=BASE + timedelta(days=days), tz="UTC")


class FakeEmbedder:
    """Maps keywords to axes so meaning-similar texts get similar vectors without any shared word."""
    AXES = {"stressed": 0, "overwhelmed": 0, "too much": 0, "garden": 1, "walk": 2}

    def __init__(self, fail=None):
        self.calls, self.fail = [], fail

    def embed(self, texts):
        if self.fail:
            raise self.fail
        self.calls.append(len(texts))
        out = []
        for t in texts:
            v = [0.05] * embeddings.DIMENSIONS
            for word, axis in self.AXES.items():
                if word in t.lower():
                    v[axis] = 1.0
            n = sum(x * x for x in v) ** 0.5
            out.append([x / n for x in v])
        return out


def test_fill_missing_embeds_once_and_edit_invalidates(conn):
    a = add(conn, "so stressed", 0)
    add(conn, "garden day", 1)
    emb = FakeEmbedder()
    assert embeddings.fill_missing(conn, emb) == 2
    assert embeddings.fill_missing(conn, emb) == 0  # nothing left, no extra call
    entries.update_entry(conn, a, text="calm now", mood=None, energy=None)
    assert embeddings.fill_missing(conn, emb) == 1  # only the edited one


def test_semantic_neighbours_find_meaning_without_shared_words(conn):
    old = add(conn, "everything feels like too much", 0)
    add(conn, "planted things in the garden", 1)
    new = add(conn, "I am so stressed", 5)
    embeddings.fill_missing(conn, FakeEmbedder())
    assert [i for i, _ in embeddings.semantic_neighbours(conn, new, 5)] == [old]
    assert links.find_candidates(conn, new)[0]["id"] == old  # FTS alone finds nothing here


def test_embedding_failure_falls_back_to_keywords(conn):
    old = add(conn, "dreading the tough talk with Sam", 0)
    new = add(conn, "had the tough talk with Sam", 3)
    llm = FakeLLM([{"links": [{"id": old, "type": "follow_up", "reason": "r"}]}])
    emb = FakeEmbedder(fail=LLMError("x", retryable=True, reason="timeout"))
    assert links.suggest_for_entry(conn, llm, new, emb) == 1


def test_deleting_entry_removes_its_embedding(conn):
    a = add(conn, "stressed", 0)
    embeddings.fill_missing(conn, FakeEmbedder())
    entries.delete_entry(conn, a)
    assert conn.execute("SELECT COUNT(*) FROM entry_embeddings").fetchone()[0] == 0


def test_pack_roundtrip_and_dot():
    v = [0.6, 0.8]
    assert list(embeddings.unpack(embeddings.pack(v))) == pytest.approx(v)
    assert embeddings.dot(v, v) == pytest.approx(1.0)


def _client(handler):
    s = LlmSettings(api_key="k", base_url="https://x.test/v1", model="m")
    return embeddings.Embedder(s, transport=httpx.MockTransport(handler))


def test_embedder_normalises_and_sends_dimensions():
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 2]}, {"index": 0, "embedding": [3, 4]}]})

    out = _client(handler).embed(["a", "b"])
    assert seen["dimensions"] == embeddings.DIMENSIONS and seen["model"] == embeddings.DEFAULT_MODEL
    assert out == [pytest.approx([0.6, 0.8]), pytest.approx([0, 1])]


@pytest.mark.parametrize("status,reason", [(429, "http_429"), (500, "http_500"), (400, "http_400")])
def test_embedder_errors_have_reason_codes(status, reason):
    with pytest.raises(LLMError) as e:
        _client(lambda r: httpx.Response(status, json={})).embed(["a"])
    assert e.value.reason == reason


def test_embedder_bad_shape_and_no_key():
    with pytest.raises(LLMError) as e:
        _client(lambda r: httpx.Response(200, json={"data": []})).embed(["a"])
    assert e.value.reason == "bad_shape"
    with pytest.raises(LLMError) as e:
        embeddings.Embedder(LlmSettings(api_key=None, base_url="https://x.test", model="m")).embed(["a"])
    assert e.value.reason == "no_key"
