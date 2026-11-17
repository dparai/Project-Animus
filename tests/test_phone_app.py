"""Installed-app support: manifest, icons, service worker, and the phone tab bar.

The rule these tests hold: the device may keep public design files only. Journal pages are never
cacheable and the worker never stores anything from outside /static/.
"""
import json
import re
from pathlib import Path

import pytest

from app.main import create_app
from tests.auth_helpers import anon_client, login_client

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "phone.db"), allowed_hosts=["testserver"], start_scheduler=False)


def test_manifest_is_public_and_points_at_real_icons(app):
    client = anon_client(app)
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(r.text)
    assert manifest["start_url"] == "/" and manifest["scope"] == "/" and manifest["display"] == "standalone"
    assert any(i["purpose"] == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        png = client.get(icon["src"])
        assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200


def test_manifest_and_worker_hold_no_secrets():
    for name in ("manifest.webmanifest", "sw.js", "offline.html"):
        text = (STATIC / name).read_text(encoding="utf-8").lower()
        for word in ("api_key", "password", "token", "secret", "duckdns", "@gmail"):
            assert word not in text, (name, word)


def test_service_worker_headers(app):
    r = anon_client(app).get("/static/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert r.headers["service-worker-allowed"] == "/"
    assert r.headers["cache-control"] == "no-cache"  # the browser must re-check it for updates
    other = anon_client(app).get("/static/theme.css")
    assert "service-worker-allowed" not in other.headers
    assert other.headers["cache-control"] == "public, max-age=86400"


def test_service_worker_only_caches_static_files():
    js = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "request.method !== 'GET'" in js
    assert "url.pathname.startsWith('/static/')" in js
    # exactly one place writes to the cache, and it sits behind the /static/ check
    assert js.count("cache.put(") == 1 and js.index("if (isStatic(url))") < js.index("cache.put(")
    assert js.count("cache.put(") + js.count("cache.add(") == 2
    navigate = js[js.index("request.mode === 'navigate'"):]
    assert "cache.put" not in navigate and "cache.add" not in navigate


def test_journal_pages_stay_uncacheable(app):
    client = login_client(app)
    for path in ("/", "/entries", "/ask", "/memory"):
        assert client.get(path).headers["cache-control"] == "no-store", path


def test_csp_allows_only_same_origin_worker_and_manifest(app):
    csp = login_client(app).get("/entries").headers["content-security-policy"]
    assert "worker-src 'self'" in csp and "manifest-src 'self'" in csp
    script = re.search(r"script-src ([^;]*)", csp).group(1)
    assert "'unsafe-inline'" not in script and "'unsafe-eval'" not in script


def test_pages_link_the_manifest_and_register_the_worker(app):
    html = login_client(app).get("/entries").text
    assert 'rel="manifest" href="/static/manifest.webmanifest"' in html
    assert 'rel="apple-touch-icon"' in html
    assert "serviceWorker.register('/static/sw.js', { scope: '/' })" in html
    login = anon_client(app).get("/login").text
    assert 'rel="manifest"' in login and "serviceWorker.register" in login


def test_every_inline_script_carries_the_nonce(app):
    for html in (login_client(app).get("/entries").text, anon_client(app).get("/login").text):
        for tag in re.findall(r"<script\b[^>]*>", html):
            assert "src=" in tag or "nonce=" in tag, tag


def test_phone_bar_has_five_tabs_and_a_more_sheet(app):
    html = login_client(app).get("/memory").text
    bar = html[html.index('class="topnav"'):html.index("</header>")]
    assert bar.count('class="navlink') == 5
    assert 'id="more-open"' in bar and "navlink here" in bar  # Memory lives under More
    assert "/logout" not in bar
    sheet = html[html.index('id="more-sheet"'):html.index("</dialog>")]
    assert 'href="/people"' in bar and 'href="/reflections"' not in bar
    for target in ('href="/reflections"', 'href="/counselor"', 'href="/memory"', 'action="/logout"',
                   'name="csrf_token"'):
        assert target in sheet, target


def test_offline_page_is_static_and_script_free(app):
    r = anon_client(app).get("/static/offline.html")
    assert r.status_code == 200 and "<script" not in r.text.lower()
