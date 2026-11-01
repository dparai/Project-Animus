from datetime import datetime, timezone

import pytest

from app import db, focus, memory
from tests.fake_llm import FakeLLM
from tests.test_dashboard import make_client, save_weekly

NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
CROSS = {"Origin": "https://evil.example"}
SAME = {"Origin": "http://localhost:8100"}


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "csrf.db")


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)


@pytest.fixture
def client(db_path):
    return make_client(db_path, llm=FakeLLM())


@pytest.fixture
def seeded(db_path):
    rid = save_weekly(db_path)
    c = db.connect(db_path)
    fid = focus.add(c, "Go to the gym twice.", None, NOW)
    nid = memory.add_note(c, kind="note", text="keep me", now=NOW)
    c.close()
    return {"rid": rid, "fid": fid, "nid": nid}


def snapshot(db_path):
    c = db.connect(db_path)
    try:
        q = lambda sql: c.execute(sql).fetchall()
        return (
            [tuple(r) for r in q("SELECT * FROM entries")],
            [tuple(r) for r in q("SELECT * FROM ai_memory")],
            [tuple(r) for r in q("SELECT * FROM reflection_jobs")],
            [tuple(r) for r in q("SELECT * FROM focus_areas")],
            [tuple(r) for r in q("SELECT * FROM settings")] if _has(c, "settings") else [],
        )
    finally:
        c.close()


def _has(c, table):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is not None


def unsafe_calls(client, s, headers):
    rid, fid, nid = s["rid"], s["fid"], s["nid"]
    return {
        "entries": lambda: client.post("/entries", data={"text": "hello"}, headers=headers),
        "generate": lambda: client.post("/reflections/generate?kind=weekly", headers=headers),
        "focus": lambda: client.post(f"/focus/{fid}/status", data={"status": "done"}, headers=headers),
        "correct": lambda: client.post(f"/reflections/{rid}/claims/w1/correct", data={"note": "x"}, headers=headers),
        "memory": lambda: client.delete(f"/memory/{nid}", headers=headers),
        "timezone": lambda: client.post("/timezone", data={"tz": "Europe/Paris"}, headers=headers),
    }


@pytest.mark.parametrize("name", ["entries", "generate", "focus", "correct", "memory", "timezone"])
def test_cross_site_origin_is_refused_and_nothing_written(client, db_path, seeded, name):
    before = snapshot(db_path)
    r = unsafe_calls(client, seeded, CROSS)[name]()
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("text/plain")
    assert snapshot(db_path) == before


@pytest.mark.parametrize("name,ok", [("entries", 200), ("generate", 200), ("focus", 200),
                                     ("correct", 200), ("memory", 200), ("timezone", 204)])
def test_same_origin_behaves_normally(client, db_path, seeded, name, ok):
    assert unsafe_calls(client, seeded, SAME)[name]().status_code == ok


def test_no_origin_headers_are_allowed(client, db_path, seeded):
    assert client.post("/entries", data={"text": "hello"}).status_code == 200


@pytest.mark.parametrize("origin", ["null", "", "http://", "https://localhost.evil.example"])
def test_null_and_malformed_origin_refused(client, db_path, seeded, origin):
    before = snapshot(db_path)
    assert client.post("/entries", data={"text": "hello"}, headers={"Origin": origin}).status_code == 403
    assert snapshot(db_path) == before


def test_origin_port_is_ignored_and_127_allowed(client):
    for origin in ("http://127.0.0.1:8100", "http://localhost", "http://testserver"):
        assert client.post("/entries", data={"text": "hi"}, headers={"Origin": origin}).status_code == 200


def test_sec_fetch_site_header(client, db_path, seeded):
    before = snapshot(db_path)
    r = client.post("/entries", data={"text": "hello"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert snapshot(db_path) == before
    for value in ("same-origin", "same-site", "none"):
        assert client.post("/entries", data={"text": "ok"}, headers={"Sec-Fetch-Site": value}).status_code == 200


def test_get_with_cross_site_origin_is_not_checked(client):
    assert client.get("/", headers={**CROSS, "Sec-Fetch-Site": "cross-site"}).status_code == 200
    assert client.get("/entries", headers=CROSS).status_code == 200
