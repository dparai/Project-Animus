"""Security headers and the CSP nonce on every response."""
import re

import pytest

from app import auth
from app.main import create_app
from tests.auth_helpers import anon_client, login_client


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "headers.db"), allowed_hosts=["testserver"], start_scheduler=False)


def _assert_base(r):
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-frame-options"] == "DENY"
    csp = r.headers["content-security-policy"]
    for part in ("default-src 'self'", "object-src 'none'", "base-uri 'self'", "form-action 'self'",
                 "frame-ancestors 'none'", "font-src 'self'",
                 "style-src 'self' 'unsafe-inline'"):
        assert part in csp, part
    script = re.search(r"script-src ([^;]*)", csp).group(1)
    assert "'unsafe-inline'" not in script and "'unsafe-eval'" not in script
    return csp


def test_security_headers_on_html_and_errors(app):
    c = login_client(app, follow_redirects=False)
    anon = anon_client(app)
    responses = [
        c.get("/"),                                   # page
        c.get("/entries/999"),                        # 404
        c.post("/entries", data={"text": " "}),       # 422
        c.post("/entries", data={"text": "x"}, headers={"X-CSRF-Token": "bad"}),  # 403 csrf
        c.post("/entries", headers={"Origin": "https://evil.example"}),           # 403 guard
        anon.get("/"),                                # 303 to login
        anon.get("/login"),
        anon.post("/login", data={"password": "wrong"}),  # 401
        c.get("/healthz"),
    ]
    assert [r.status_code for r in responses] == [200, 404, 422, 403, 403, 303, 200, 401, 200]
    for r in responses:
        _assert_base(r)


def test_unconfigured_503_has_headers(tmp_path):
    app = create_app(str(tmp_path / "u.db"), allowed_hosts=["testserver"], start_scheduler=False,
                     auth=auth.AuthSettings())
    r = anon_client(app).get("/")
    assert r.status_code == 503
    _assert_base(r)


def test_inline_scripts_carry_this_responses_nonce(app):
    c = login_client(app)
    nonces = set()
    for path in ("/", "/entries", "/reflections", "/memory", "/ask", "/counselor"):
        r = c.get(path)
        nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", r.headers["content-security-policy"]).group(1)
        nonces.add(nonce)
        scripts = re.findall(r"<script\b([^>]*)>", r.text)
        assert scripts, path
        for attrs in scripts:
            assert "src=" in attrs or f'nonce="{nonce}"' in attrs, (path, attrs)
        assert re.search(r"\son[a-z]+=", r.text) is None, path  # no inline event handlers
    login = anon_client(app).get("/login")
    nonce = re.search(r"'nonce-([^']+)'", login.headers["content-security-policy"]).group(1)
    assert f'<script nonce="{nonce}">' in login.text
    nonces.add(nonce)
    assert len(nonces) == 7  # fresh per response


def test_cache_control_no_store_on_authenticated_html(app):
    c = login_client(app)
    for path in ("/", "/entries", "/reflections", "/memory", "/ask", "/counselor", "/focus"):
        r = c.get(path)
        assert r.headers["content-type"].startswith("text/html"), path
        assert r.headers["cache-control"] == "no-store", path
    assert anon_client(app).get("/login").headers["cache-control"] == "no-store"
