import re

import pytest

from app import db, entries, tz
from app.main import create_app
from tests.auth_helpers import login_client


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "write.db")


@pytest.fixture
def client(db_path):
    return login_client(create_app(db_path, allowed_hosts=["testserver"]))


def _rows(db_path):
    c = db.connect(db_path)
    try:
        return entries.list_entries(c)
    finally:
        c.close()


def test_write_screen_has_checkin_then_text(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    assert len(re.findall(r'name="mood"', html)) == 5
    assert 'type="range"' in html and 'min="1"' in html and 'max="10"' in html
    assert "<textarea" in html
    assert "Save" in html
    assert html.index('name="mood"') < html.index("<textarea")
    assert html.index('type="range"') < html.index("<textarea")
    assert html.index("<textarea") < html.index("Save")


def test_save_valid_entry(client, db_path):
    r = client.post("/entries", data={"text": "A quiet day", "mood": "good", "energy": "7"})
    assert r.status_code == 200
    assert "Saved" in r.text
    rows = _rows(db_path)
    assert len(rows) == 1
    assert rows[0]["text"] == "A quiet day"
    assert rows[0]["mood"] == "good"
    assert rows[0]["energy_score"] == 7


def test_save_without_mood_or_energy(client, db_path):
    r = client.post("/entries", data={"text": "Just words", "mood": "", "energy": ""})
    assert r.status_code == 200
    rows = _rows(db_path)
    assert rows[0]["mood"] is None and rows[0]["energy_score"] is None


def test_blank_text_returns_422_and_saves_nothing(client, db_path):
    r = client.post("/entries", data={"text": "   \n ", "mood": "good", "energy": "5"})
    assert r.status_code == 422
    assert "text/html" in r.headers["content-type"]
    assert _rows(db_path) == []


def test_energy_out_of_range_422(client, db_path):
    for bad in ("0", "11", "abc"):
        r = client.post("/entries", data={"text": "hello", "energy": bad})
        assert r.status_code == 422
    assert _rows(db_path) == []


def test_unknown_mood_422(client, db_path):
    r = client.post("/entries", data={"text": "hello", "mood": "ecstatic"})
    assert r.status_code == 422
    assert _rows(db_path) == []


def test_timezone_post_persists(client, db_path):
    r = client.post("/timezone", data={"tz": "Europe/London"})
    assert r.status_code == 204
    c = db.connect(db_path)
    assert tz.get_timezone(c) == "Europe/London"
    c.close()
    assert client.post("/timezone", data={"tz": "Europe/London"}).status_code == 204


def test_bad_timezone_ignored(client, db_path):
    client.post("/timezone", data={"tz": "Europe/London"})
    r = client.post("/timezone", data={"tz": "Not/AZone"})
    assert r.status_code == 422
    c = db.connect(db_path)
    assert tz.get_timezone(c) == "Europe/London"
    c.close()


def test_write_form_posts_without_htmx(client):
    html = client.get("/").text
    forms = re.findall(r"<form\b[^>]*>", html)
    assert forms
    entry_form = next(f for f in forms if 'id="entry-form"' in f)
    assert 'method="post"' in entry_form and 'action="/entries"' in entry_form
    assert not any(re.search(r'method="get"', f, re.I) for f in forms)


def test_slider_registers_on_more_than_input(client):
    html = client.get("/").text
    for event in ("input", "change", "pointerdown", "pointerup", "keydown"):
        assert f"'{event}'" in html, event
    assert 'class="untouched"' in html  # untouched slider still has no name, so it saves null


def test_htmx_script_is_local_and_has_integrity(client):
    html = client.get("/").text
    tag = re.search(r'<script[^>]*htmx-2\.0\.4\.min\.js[^>]*>', html).group(0)
    assert 'src="/static/htmx-2.0.4.min.js"' in tag and 'integrity="sha384-' in tag
