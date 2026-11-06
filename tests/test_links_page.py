from datetime import datetime, timedelta, timezone

import pytest

from app import db, entries
from app.main import create_app
from tests.auth_helpers import login_client
from tests.fake_llm import FakeLLM

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _seed(path):
    c = db.connect(path)
    old = entries.create_entry(c, text="dreading the tough talk with Sam", mood=None, energy=None, now=BASE, tz="UTC")
    new = entries.create_entry(c, text="had the tough talk with Sam", mood=None, energy=None,
                               now=BASE + timedelta(days=3), tz="UTC")
    c.execute("INSERT INTO entry_links(from_entry_id,to_entry_id,link_type,source,status,reason,created_at)"
              " VALUES (?,?,?,?,?,?,?)", (new, old, "follow_up", "ai", "suggested", "You dreaded this.", "2026-10-04T00:00:00Z"))
    c.commit()
    c.close()
    return old, new


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / "p.db")
    app = create_app(path, allowed_hosts=["testserver"])
    app.state.llm = FakeLLM()
    old, new = _seed(path)
    return login_client(app), path, old, new


def test_suggestion_shown_then_confirmed(setup):
    client, path, old, new = setup
    page = client.get("/entries").text
    assert "Suggested: follow-up to" in page and "You dreaded this." in page and "Link them" in page
    r = client.post(f"/entries/{new}/links/1/confirm")
    assert r.status_code == 200 and "Follow-up to" in r.text and "Link them" not in r.text
    assert "Followed up by" in client.get(f"/entries/{old}").text


def test_dismiss_hides_suggestion(setup):
    client, path, old, new = setup
    r = client.post(f"/entries/{new}/links/1/dismiss")
    assert r.status_code == 200 and "Suggested" not in r.text
    assert "Suggested" not in client.get("/entries").text


def test_wrong_entry_or_link_is_404(setup):
    client, path, old, new = setup
    assert client.post(f"/entries/{old}/links/1/confirm").status_code == 404
    assert client.post(f"/entries/{new}/links/99/confirm").status_code == 404


def test_saving_entry_schedules_suggestions(tmp_path):
    path = str(tmp_path / "s.db")
    app = create_app(path, allowed_hosts=["testserver"])
    c = db.connect(path)
    old = entries.create_entry(c, text="dreading the tough talk with Sam", mood=None, energy=None, now=BASE, tz="UTC")
    c.close()
    app.state.llm = FakeLLM([{"links": [{"id": old, "type": "follow_up", "reason": "r"}]}])
    client = login_client(app)
    assert client.post("/entries", data={"text": "had the tough talk with Sam", "mood": "", "energy": ""}).status_code == 200
    c = db.connect(path)
    assert c.execute("SELECT COUNT(*) FROM entry_links WHERE status='suggested'").fetchone()[0] == 1
