"""Local static assets: HTMX and Tailwind served from /static, no third-party script anywhere."""
import hashlib
import re
from pathlib import Path

import pytest

from app.main import create_app
from tests.auth_helpers import anon_client, login_client

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def app(tmp_path):
    return create_app(str(tmp_path / "static.db"), allowed_hosts=["testserver"], start_scheduler=False)


def _recorded_hashes():
    text = (STATIC / "VERSIONS.md").read_text()
    return dict(re.findall(r"`([\w.\-]+\.js)`.*?\b([0-9a-f]{64})\b", text))


def test_static_htmx_served_and_hash_matches_recorded(app):
    client = anon_client(app)  # no session needed
    for name in ("htmx-2.0.4.min.js", "tailwind-play.js"):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=86400"
        assert "javascript" in r.headers["content-type"]
        assert r.headers["x-content-type-options"] == "nosniff"
        assert hashlib.sha256(r.content).hexdigest() == _recorded_hashes()[name]
        assert r.content == (STATIC / name).read_bytes()


def test_htmx_matches_integrity_attribute_in_base(app):
    base = (STATIC.parent / "templates" / "base.html").read_text()
    m = re.search(r'src="/static/htmx-2.0.4.min.js" integrity="sha384-([^"]+)"', base)
    assert m
    import base64
    digest = base64.b64encode(hashlib.sha384((STATIC / "htmx-2.0.4.min.js").read_bytes()).digest()).decode()
    assert digest == m.group(1)


PAGES = ["/", "/entries", "/reflections", "/memory", "/ask", "/counselor"]


def test_no_external_script_src_in_any_rendered_page(app):
    pages = [(anon_client(app), "/login")] + [(login_client(app), p) for p in PAGES]
    for client, path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        srcs = re.findall(r"<script\b[^>]*\bsrc=[\"']([^\"']+)", r.text)
        assert srcs, path
        for src in srcs:
            assert src.startswith("/static/"), (path, src)
        assert "cdn.tailwindcss.com" not in r.text and "unpkg.com" not in r.text


@pytest.mark.parametrize("path", [
    "/static/../main.py", "/static/%2e%2e/main.py", "/static/..%2fmain.py", "/static/%2e%2e%2fmain.py",
    "/static/../../.env", "/static/....//main.py", "/static//etc/passwd", "/static/%00",
])
def test_static_traversal_refused(app, path):
    for client in (anon_client(app), login_client(app)):
        r = client.get(path)
        # anonymous: normalised away from /static, so the login redirect; signed in: 404
        assert r.status_code in (303, 400, 403, 404)
        assert b"import" not in r.content and b"root:" not in r.content


def test_static_no_directory_listing(app):
    client = login_client(app, follow_redirects=True)
    for path in ("/static/", "/static"):
        r = client.get(path)
        assert r.status_code == 404
        assert "htmx" not in r.text and "tailwind" not in r.text
    assert anon_client(app).get("/static/").status_code == 404


def test_static_post_405(app):
    # The mounted file server itself answers 405 to anything but GET/HEAD ...
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.main import CachedStaticFiles
    bare = FastAPI()
    bare.mount("/static", CachedStaticFiles(directory=str(STATIC)))
    for method in ("post", "put", "delete", "patch"):
        assert getattr(TestClient(bare), method)("/static/htmx-2.0.4.min.js").status_code == 405, method
    # ... and in the real app an unsafe request never reaches it (session-less path: CSRF refuses, 403).
    client = login_client(app)
    for method in ("post", "put", "delete", "patch"):
        r = getattr(client, method)("/static/htmx-2.0.4.min.js")
        assert r.status_code in (403, 405), method
    assert (STATIC / "htmx-2.0.4.min.js").exists()


def test_static_unauthenticated_post_refused(app):
    assert anon_client(app).post("/static/htmx-2.0.4.min.js").status_code in (403, 405)


def test_static_head_ok_and_missing_404(app):
    client = anon_client(app)
    assert client.head("/static/htmx-2.0.4.min.js").status_code == 200
    assert client.get("/static/nope.js").status_code == 404


def test_csp_allows_only_self_scripts(app):
    for client, path in ((anon_client(app), "/login"), (login_client(app), "/")):
        csp = client.get(path).headers["content-security-policy"]
        script = next(d for d in csp.split("; ") if d.startswith("script-src"))
        assert re.fullmatch(r"script-src 'self' 'nonce-[\w\-]+'", script)
        assert "unsafe-eval" not in csp and "unsafe-inline" not in script
        assert "unpkg" not in csp and "tailwindcss" not in csp


def test_versions_file_records_both_hashes():
    hashes = _recorded_hashes()
    for name in ("htmx-2.0.4.min.js", "tailwind-play.js"):
        assert hashlib.sha256((STATIC / name).read_bytes()).hexdigest() == hashes[name]


FONTS = {
    "inter-latin-variable.woff2": "76658c751953637fc8bd972c0c8581103ff50fd1a301c016c6ee8cbe2d651db6",
    "instrument-serif-400-latin.woff2": "5eb09b5ac0e28b67c2f041c8ba6d244604ca0c0980d65912ab2d47fed84ddc31",
}


@pytest.mark.parametrize("name,digest", sorted(FONTS.items()))
def test_font_files_served_from_static_fonts(app, name, digest):
    r = anon_client(app).get(f"/static/fonts/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "font/woff2"
    assert r.headers["cache-control"] == "public, max-age=86400"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content[:4] == b"wOF2"
    assert hashlib.sha256(r.content).hexdigest() == digest
    assert len(r.content) < 60_000
    assert digest in (STATIC / "VERSIONS.md").read_text()
    assert f"fonts/{name}" in (STATIC / "VERSIONS.md").read_text()


def test_no_google_font_host_in_pages_or_csp(app):
    pages = [(anon_client(app), "/login")] + [(login_client(app), p) for p in PAGES]
    for client, path in pages:
        r = client.get(path)
        assert r.status_code == 200, path
        for text in (r.text, r.headers["content-security-policy"]):
            assert "googleapis" not in text and "gstatic" not in text, path
        csp = r.headers["content-security-policy"]
        assert "font-src 'self';" in csp + ";" and "font-src 'self' " not in csp
        assert "style-src 'self' 'unsafe-inline';" in csp + ";"
        assert "/static/fonts/inter-latin-variable.woff2" in r.text
        assert "/static/fonts/instrument-serif-400-latin.woff2" in r.text
        assert "font-display: swap" in r.text


def test_font_face_urls_point_at_existing_files():
    base = (STATIC.parent / "templates" / "base.html").read_text()
    for url in re.findall(r'url\("(/static/fonts/[^"]+)"\)', base):
        assert (STATIC / url.removeprefix("/static/")).is_file(), url
