from datetime import datetime, timezone

import pytest

from app import db
from app.main import create_app
from tests.auth_helpers import anon_client
from tests.test_dashboard import make_client


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "theme.db")
    c = db.connect(path)
    db.init_db(c)
    c.close()
    return path


def _entry(path, mood, created):
    c = db.connect(path)
    c.execute(
        "INSERT INTO entries(created_at, local_date, text, mood) VALUES (?, ?, 'x', ?)",
        (created, created[:10], mood),
    )
    c.commit()
    c.close()


def test_pages_load_theme_and_glow(db_path):
    html = make_client(db_path).get("/ask").text
    assert '/static/theme.css' in html and '/static/theme.js' in html
    assert 'class="glow"' in html and 'aria-hidden="true"' in html


def test_theme_assets_served(db_path):
    client = anon_client(create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False))
    css = client.get("/static/theme.css")
    assert css.status_code == 200 and "css" in css.headers["content-type"]
    assert "prefers-reduced-motion" in css.text  # motion has an off switch
    js = client.get("/static/theme.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]


def test_theme_state_returns_recent_mood(db_path):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _entry(db_path, "good", now)
    r = make_client(db_path).get("/theme-state")
    assert r.status_code == 200 and r.json() == {"mood": "good"}
    assert "no-store" in r.headers["cache-control"]


def test_theme_state_ignores_old_and_missing(db_path):
    client = make_client(db_path)
    assert client.get("/theme-state").json() == {"mood": ""}
    _entry(db_path, "low", "2020-01-01T10:00:00Z")
    assert client.get("/theme-state").json() == {"mood": ""}


def test_theme_state_needs_login(db_path):
    client = anon_client(create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False))
    r = client.get("/theme-state", follow_redirects=False)
    assert r.status_code in (302, 303, 401)
