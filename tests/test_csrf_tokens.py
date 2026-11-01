"""CSRF tokens: every unsafe request needs the session's token (header or form field)."""
import asyncio
import html
import json
import re
from datetime import datetime, timezone

import pytest

from app import counselor, db, entries
from app.csrf import CsrfMiddleware, REFUSED
from app.main import create_app
from tests.auth_helpers import TEST_PASSWORD, anon_client, csrf_headers, login_client
from tests.fake_llm import FakeLLM
from tests.test_dashboard import save_weekly

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "tokens.db")


@pytest.fixture(autouse=True)
def no_env_key(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)


@pytest.fixture
def app(db_path):
    return create_app(db_path, allowed_hosts=["testserver"], llm=FakeLLM(), start_scheduler=False)


@pytest.fixture
def client(app):
    return login_client(app)


@pytest.fixture
def bare(app):
    """Logged in, but without the default X-CSRF-Token header."""
    c = login_client(app)
    token = c.headers.pop("X-CSRF-Token")
    c.token = token
    return c


def _count(db_path):
    c = db.connect(db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    finally:
        c.close()


def test_post_without_token_403_no_side_effect(bare, db_path):
    r = bare.post("/entries", data={"text": "fake entry"})
    assert r.status_code == 403
    assert r.text == REFUSED
    assert r.headers["content-type"].startswith("text/plain")
    assert _count(db_path) == 0


def test_wrong_token_403(bare, db_path):
    for headers, data in (
        ({"X-CSRF-Token": "not-the-token"}, {"text": "fake entry"}),
        ({"X-CSRF-Token": ""}, {"text": "fake entry"}),
        ({}, {"text": "fake entry", "csrf_token": "not-the-token"}),
        ({}, {"text": "fake entry", "csrf_token": ""}),
        ({"X-CSRF-Token": "wrong"}, {"text": "fake entry", "csrf_token": bare.token}),  # header wins
    ):
        r = bare.post("/entries", data=data, headers=headers)
        assert r.status_code == 403, (headers, data)
    assert _count(db_path) == 0


def test_token_from_another_session_refused(app, db_path):
    a, b = login_client(app), login_client(app)
    assert a.headers["X-CSRF-Token"] != b.headers["X-CSRF-Token"]
    r = a.post("/entries", data={"text": "fake"}, headers={"X-CSRF-Token": b.headers["X-CSRF-Token"]})
    assert r.status_code == 403
    assert _count(db_path) == 0


def test_htmx_header_token_accepted(bare, db_path):
    r = bare.post("/entries", data={"text": "fake entry"},
                  headers={"HX-Request": "true", "X-CSRF-Token": bare.token})
    assert r.status_code == 200
    assert _count(db_path) == 1


def test_form_field_token_accepted_and_body_replayed(bare, db_path):
    r = bare.post("/entries", data={"text": "fake replayed text", "mood": "okay", "csrf_token": bare.token})
    assert r.status_code == 200
    c = db.connect(db_path)
    try:
        row = entries.list_entries(c)[0]
    finally:
        c.close()
    assert row["text"] == "fake replayed text"  # the route saw the full body after the check


def test_multipart_without_header_refused(bare, db_path):
    r = bare.post("/entries", data={"text": "fake", "csrf_token": bare.token},
                  files={"f": ("a.txt", b"x")})
    assert r.status_code == 403
    assert _count(db_path) == 0


@pytest.mark.parametrize("method,path", [("put", "/entries/1"), ("delete", "/entries/1"),
                                         ("post", "/timezone"), ("post", "/logout"),
                                         ("patch", "/entries/1")])
def test_every_unsafe_method_needs_token(bare, db_path, method, path):
    c = db.connect(db_path)
    try:
        entries.create_entry(c, text="fake", mood=None, energy=None,
                             now=NOW,
                             tz="UTC")
    finally:
        c.close()
    r = bare.request(method.upper(), path)
    assert r.status_code == 403
    assert _count(db_path) == 1


def test_get_needs_no_token(bare):
    for path in ("/", "/entries", "/reflections", "/memory", "/ask", "/counselor"):
        assert bare.get(path).status_code == 200, path


def test_logout_needs_token(app, db_path):
    c = login_client(app, follow_redirects=False)
    token = c.headers.pop("X-CSRF-Token")
    assert c.post("/logout").status_code == 403
    assert c.get("/").status_code == 200  # still logged in: the refused logout did nothing
    r = c.post("/logout", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_post_exempt_from_csrf_but_not_from_origin_guard(app):
    c = anon_client(app)
    r = c.post("/login", data={"password": TEST_PASSWORD, "next": "/"})
    assert r.status_code == 303  # no session yet, so no token: exempt
    assert c.get("/").status_code == 200
    # the new session's token works, and a POST without it does not
    assert c.post("/timezone", data={"tz": "UTC"}).status_code == 403
    assert c.post("/timezone", data={"tz": "UTC"}, headers=csrf_headers(app, c)).status_code == 204
    other = anon_client(app)
    r = other.post("/login", data={"password": TEST_PASSWORD}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = other.post("/login", data={"password": TEST_PASSWORD}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert "set-cookie" not in r.headers


def test_same_origin_form_post_with_null_origin_passes_guard(app, bare, db_path):
    """Referrer-Policy: no-referrer makes browsers send Origin: null on same-origin form posts."""
    same = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    r = anon_client(app).post("/login", data={"password": TEST_PASSWORD}, headers=same)
    assert r.status_code == 303
    r = bare.post("/entries", data={"text": "fake", "csrf_token": bare.token}, headers=same)
    assert r.status_code == 200
    assert bare.post("/entries", data={"text": "fake"}, headers=same).status_code == 403  # token still needed
    for headers in ({"Origin": "null"}, {"Origin": "null", "Sec-Fetch-Site": "same-site"},
                    {"Origin": "null", "Sec-Fetch-Site": "cross-site"}):
        r = anon_client(app).post("/login", data={"password": TEST_PASSWORD}, headers=headers)
        assert r.status_code == 403 and "set-cookie" not in r.headers, headers
    assert _count(db_path) == 1


def _page_html(client, db_path):
    save_weekly(db_path)
    c = db.connect(db_path)
    try:
        conv = counselor.start(c, NOW)
        entries.create_entry(c, text="fake", mood=None, energy=None,
                             now=NOW,
                             tz="UTC")
    finally:
        c.close()
    pages = {}
    for path in ("/", "/entries", "/reflections", "/memory", "/ask", "/counselor", f"/counselor/{conv}",
                 "/entries/1/edit", "/focus"):
        r = client.get(path)
        assert r.status_code == 200, path
        pages[path] = r.text
    return pages


def test_every_template_form_and_hx_post_has_token(client, db_path):
    token = client.headers["X-CSRF-Token"]
    pages = _page_html(client, db_path)
    seen_forms = 0
    for path, text in pages.items():
        for form in re.findall(r"<form\b[^>]*>.*?</form>", text, flags=re.S | re.I):
            if re.search(r'method="post"', form, flags=re.I):
                seen_forms += 1
                assert f'name="csrf_token" value="{token}"' in form, (path, form[:120])
        if path in ("/entries/1/edit", "/focus"):
            continue  # fragments: the page's <body> carries the header
        body = re.search(r"<body([^>]*)>", text).group(1)
        m = re.search(r"hx-headers='([^']*)'", body)
        assert m, path
        assert json.loads(html.unescape(m.group(1))) == {"X-CSRF-Token": token}
        assert f'<meta name="csrf-token" content="{token}">' in text
    assert seen_forms >= 8  # logout x2 per page, entry form, Begin, focus, ...


def test_login_page_has_no_token_markup(app):
    text = anon_client(app).get("/login").text
    assert "hx-headers" not in text and 'name="csrf-token"' not in text


def test_every_unsafe_hx_attribute_is_under_a_body_with_token():
    """Source check: unsafe hx-* calls only live in templates rendered inside base.html's body."""
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / "app" / "templates"
    for f in templates.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        if re.search(r"hx-(post|put|patch|delete)=", text):
            assert f.name.startswith("_") or '{% extends "base.html" %}' in text, f.name
        assert "fetch(" not in text or "X-CSRF-Token" in text, f.name


def test_csrf_middleware_non_http_passes():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(CsrfMiddleware(inner)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]
