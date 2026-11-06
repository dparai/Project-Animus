import pytest

from app import db
from tests.auth_helpers import TEST_HASH, TEST_SECRET, anon_client, login_client  # noqa: F401


@pytest.fixture(autouse=True)
def fake_auth_env(monkeypatch):
    """Every create_app() without auth= reads these fake values, so apps are configured.

    Clients still need a session: build them with login_client(app) (logged in) or
    anon_client(app) (no session, redirects not followed).
    """
    monkeypatch.setenv("ANIMUS_PASSWORD_HASH", TEST_HASH.decode("ascii"))
    monkeypatch.setenv("ANIMUS_SESSION_SECRET", TEST_SECRET)
    for prefix in ("LLM_", "NVIDIA_"):  # a real key in the shell must never reach tests
        for name in ("API_KEY", "BASE_URL", "MODEL", "MODEL_INTERACTIVE", "MODEL_ESCALATE"):
            monkeypatch.delenv(prefix + name, raising=False)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    yield c
    c.close()


def _fake_entry(id, date, content="fake text", mood=None, energy=None):
    return {"id": id, "content": content, "date": date, "mood": mood, "energyLevel": energy}


@pytest.fixture
def write_export(tmp_path):
    def _write(entries, mood_entries=()):
        import json

        p = tmp_path / "fake-export.json"
        p.write_text(
            json.dumps(
                {"data": {"entries": list(entries), "moodEntries": list(mood_entries),
                          "recaps": [{"id": "r1", "content": "ignored"}]}}
            ),
            encoding="utf-8",
        )
        return str(p)

    return _write


@pytest.fixture
def fake_entry():
    return _fake_entry
