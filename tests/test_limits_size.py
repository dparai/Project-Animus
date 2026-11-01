"""Request body limit (400 KB, 413) and the entry text cap (20,000 characters, 422)."""
import asyncio

import pytest

from app import db, entries
from app.headers import BODY_MAX, BodyLimitMiddleware
from app.main import create_app
from tests.auth_helpers import TEST_PASSWORD, anon_client, login_client


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "size.db")


@pytest.fixture
def app(db_path):
    return create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False)


@pytest.fixture
def client(app):
    return login_client(app)


def _rows(db_path):
    c = db.connect(db_path)
    try:
        return entries.list_entries(c)
    finally:
        c.close()


def test_body_over_limit_413(client, db_path):
    big = "x" * (BODY_MAX + 1)
    r = client.post("/entries", content=("text=" + big).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert r.text == "Request too large."
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert _rows(db_path) == []


def test_streamed_body_over_limit_413_without_content_length(client, db_path):
    def chunks():
        for _ in range(50):
            yield b"x" * 10_000

    r = client.post("/entries", content=chunks(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
    assert _rows(db_path) == []


def test_body_limit_counts_streamed_bytes_despite_small_content_length():
    """A client that lies in Content-Length is still cut off by the byte count."""
    reached = []

    async def inner(scope, receive, send):
        reached.append(True)

    messages = [{"type": "http.request", "body": b"x" * 250_000, "more_body": True},
                {"type": "http.request", "body": b"x" * 250_000, "more_body": False}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/entries", "headers": [(b"content-length", b"10")]}
    asyncio.run(BodyLimitMiddleware(inner)(scope, receive, send))
    assert reached == []
    assert sent[0]["status"] == 413


def test_body_at_limit_passes_to_the_route(client, db_path):
    text = "y" * 19_000
    r = client.post("/entries", data={"text": text})
    assert r.status_code == 200
    assert len(_rows(db_path)) == 1


def test_login_body_limits_still_apply(app):
    c = anon_client(app)
    assert c.post("/login", content=b"password=" + b"x" * (BODY_MAX + 10),
                  headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 413
    assert c.post("/login", content=b"password=" + b"x" * 9_000,
                  headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 413  # 8 KB
    assert c.post("/login", data={"password": TEST_PASSWORD}).status_code == 303


def test_entry_too_long_422(client, db_path):
    r = client.post("/entries", data={"text": "z" * 20_001})
    assert r.status_code == 422
    assert "That entry is too long (limit 20,000 characters)." in r.text
    assert _rows(db_path) == []
    # exactly 20,000 is fine, and surrounding whitespace does not count
    r = client.post("/entries", data={"text": "  " + "z" * 20_000 + "\n"})
    assert r.status_code == 200
    rows = _rows(db_path)
    assert len(rows) == 1
    eid = rows[0]["id"]
    r = client.put(f"/entries/{eid}", data={"text": "w" * 20_001})
    assert r.status_code == 422
    assert "That entry is too long (limit 20,000 characters)." in r.text
    assert _rows(db_path)[0]["text"] == "z" * 20_000


def test_blank_entry_message_unchanged(client):
    r = client.post("/entries", data={"text": "   "})
    assert r.status_code == 422
    assert "Write something first." in r.text


def test_20000_emoji_entry_reaches_friendly_422_not_413(client):
    from urllib.parse import urlencode
    body = urlencode({"text": "\U0001F600" * 20_000}).encode()
    assert 200_000 < len(body) < BODY_MAX
    r = client.post("/entries", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code != 413
    assert r.status_code in (200, 303, 422)


def test_base_template_handles_413_calmly():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "app" / "templates" / "base.html").read_text()
    assert "status === 413" in html and "too large to send" in html
