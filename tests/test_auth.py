import logging
import os
import re
import stat
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import bcrypt
import pytest

from app import auth, db, entries
from app.limits import LoginLimiter
from app.main import create_app
from scripts import set_password
from tests.auth_helpers import TEST_PASSWORD, TEST_SECRET, TEST_SETTINGS, anon_client, csrf_headers, login_client

LOOPBACK = ("127.0.0.1", 40000)


class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "auth.db")


def make_app(db_path, settings=TEST_SETTINGS, **kwargs):
    return create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False, auth=settings, **kwargs)


def post_login(client, password=TEST_PASSWORD, next_url=None, **kwargs):
    data = {"password": password}
    if next_url is not None:
        data["next"] = next_url
    return client.post("/login", data=data, **kwargs)


def same_page(text):
    """The page with its per-response CSP nonce blanked (it is the only part that may differ)."""
    return re.sub(r'nonce="[^"]*"', 'nonce=""', text)


def cookie_header(response):
    return [v for k, v in response.headers.multi_items() if k == "set-cookie"]


@pytest.fixture
def records():
    """Collect animus.auth log records (the animus logger does not propagate to caplog)."""
    got = []

    class _Keep(logging.Handler):
        def emit(self, record):
            got.append(record.getMessage())

    handler = _Keep()
    logger = logging.getLogger("animus.auth")
    logger.addHandler(handler)
    yield got
    logger.removeHandler(handler)


def test_wrong_password_401_same_body_as_unknown(db_path, records):
    client = anon_client(make_app(db_path))
    bodies = []
    for wrong in ("not the password", "fake test password 124", "パスワード違い🙂", TEST_PASSWORD.upper()):
        r = post_login(client, wrong)
        assert r.status_code == 401
        assert "That didn&#39;t work." in r.text or "That didn't work." in r.text
        assert wrong not in r.text
        assert "set-cookie" not in r.headers
        bodies.append(same_page(r.text))
    assert len(set(bodies)) == 1
    joined = " ".join(records)
    assert "reason=bad_password" in joined
    assert TEST_PASSWORD.upper() not in joined and "not the password" not in joined
    assert "127.0.0.1" not in joined and "testclient" not in joined


def test_overlong_and_empty_password_rejected_without_hashing_error(db_path, monkeypatch, records):
    client = anon_client(make_app(db_path))
    reference = same_page(post_login(client, "simply wrong").text)
    for value in ("", "a" * 201, TEST_PASSWORD + "x" * 300, "é" * 1000):
        r = post_login(client, value)
        assert r.status_code == 401
        assert same_page(r.text) == reference
    assert "reason=empty" in " ".join(records) and "reason=too_long" in " ".join(records)
    # Constant work: one bcrypt check for every input and configuration, never a ValueError.
    calls = []
    real = bcrypt.checkpw
    monkeypatch.setattr(auth.bcrypt, "checkpw", lambda pw, h: calls.append(1) or real(pw, h))
    unset = auth.AuthSettings()
    for settings in (TEST_SETTINGS, unset):
        for value in ("", "x" * 201, "y" * 72 + "z", TEST_PASSWORD):
            assert auth.verify_password(settings, value) is (settings is TEST_SETTINGS and value == TEST_PASSWORD)
    assert len(calls) == 8
    # Long and unicode passwords hash and verify (bcrypt's 72-byte limit is never hit).
    long_pw = "ü" * 200
    s = auth.AuthSettings(password_hash=auth.hash_password(long_pw, cost=4), secret=TEST_SECRET)
    assert auth.verify_password(s, long_pw)
    assert not auth.verify_password(s, "ü" * 199)
    with pytest.raises(ValueError):
        auth.hash_password("", cost=4)


def test_right_password_sets_cookie_flags(db_path):
    app = make_app(db_path)
    r = post_login(anon_client(app))
    assert r.status_code == 303 and r.headers["location"] == "/"
    (cookie,) = cookie_header(r)
    flags = [p.strip().lower() for p in cookie.split(";")]
    assert flags[0].startswith(auth.COOKIE_NAME + "=")
    assert "httponly" in flags and "samesite=lax" in flags and "max-age=86400" in flags and "path=/" in flags
    assert "secure" not in flags
    https = {"X-Forwarded-Proto": "https"}
    via_proxy = post_login(anon_client(app, client=LOOPBACK), headers=https)
    assert "secure" in [p.strip().lower() for p in cookie_header(via_proxy)[0].split(";")]
    spoofed = post_login(anon_client(app, client=("203.0.113.7", 5000)), headers=https)
    assert "secure" not in [p.strip().lower() for p in cookie_header(spoofed)[0].split(";")]
    # The cookie opens the app.
    client = anon_client(app)
    client.cookies.set(auth.COOKIE_NAME, cookie.split(";")[0].split("=", 1)[1])
    page = client.get("/")
    assert page.status_code == 200 and 'action="/logout"' in page.text


def test_unauthenticated_redirects_to_login_with_next(db_path):
    client = anon_client(make_app(db_path))
    r = client.get("/entries?before=2026-11-01T10:00:00Z_5")
    assert r.status_code == 303
    loc = urlsplit(r.headers["location"])
    assert loc.path == "/login"
    assert parse_qs(loc.query)["next"] == ["/entries?before=2026-11-01T10:00:00Z_5"]
    assert client.get("/").headers["location"] == "/login"
    for path in ("/reflections", "/memory", "/ask", "/counselor", "/nope"):
        assert client.get(path).status_code == 303
    # Unsafe requests are refused and write nothing.
    r = client.post("/entries", data={"text": "fake text"})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    c = db.connect(db_path)
    assert c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
    c.close()
    # After logging in with that next, the owner lands where they were going.
    r = post_login(client, next_url="/entries?before=2026-11-01T10:00:00Z_5")
    assert r.headers["location"] == "/entries?before=2026-11-01T10:00:00Z_5"


def test_htmx_unauthenticated_gets_hx_redirect(db_path):
    client = anon_client(make_app(db_path))
    for method, path in (("get", "/reflections/status?kind=weekly"), ("post", "/entries"), ("delete", "/memory/1")):
        r = getattr(client, method)(path, headers={"HX-Request": "true"})
        assert r.status_code == 401
        assert r.headers["hx-redirect"] == "/login"
        assert r.text == ""


def test_tampered_expired_and_old_secret_cookies_rejected(db_path):
    app = make_app(db_path)
    now = time.time()
    good = auth.issue_session(TEST_SETTINGS, now)
    old_secret = auth.AuthSettings(password_hash=None, secret="an-older-secret-that-was-rotated-away-0000")
    flipped = good[:-3] + ("A" if good[-3] != "A" else "B") + good[-2:]
    bad = {
        "tampered": flipped,
        "truncated": good[: len(good) // 2],
        "old_secret": auth.issue_session(old_secret, now),
        "expired": auth.issue_session(TEST_SETTINGS, now - 86401),
        "future": auth.issue_session(TEST_SETTINGS, now + 3600),
        "garbage": "not-a-cookie",
        "huge": "a" * 5000,
        "payload_swap": "eyJjIjoieCJ9" + good[good.index("."):],
    }
    for name, value in bad.items():
        client = anon_client(app)
        client.cookies.set(auth.COOKIE_NAME, value)
        r = client.get("/")
        assert r.status_code == 303, name
        assert r.headers["location"] == "/login", name
        assert auth.read_session(TEST_SETTINGS, value, now) is None, name
    assert auth.read_session(TEST_SETTINGS, good, now + 86399) is not None
    assert auth.read_session(TEST_SETTINGS, good, now + 86401) is None


def test_unconfigured_fails_closed_503(db_path):
    c = db.connect(db_path)
    db.init_db(c)
    entries.create_entry(c, text="fake secret journal line", mood=None, energy=None,
                         now=datetime.now(timezone.utc), tz="UTC")
    c.close()
    partial = (
        auth.AuthSettings(),
        auth.AuthSettings(password_hash=TEST_SETTINGS.password_hash),
        auth.AuthSettings(secret=TEST_SECRET),
        auth.load_settings({"ANIMUS_PASSWORD_HASH": "plain", "ANIMUS_SESSION_SECRET": TEST_SECRET}),
        auth.load_settings({"ANIMUS_PASSWORD_HASH": TEST_SETTINGS.password_hash.decode(),
                            "ANIMUS_SESSION_SECRET": "short"}),
    )
    for settings in partial:
        client = anon_client(make_app(db_path, settings=settings))
        client.cookies.set(auth.COOKIE_NAME, auth.issue_session(TEST_SETTINGS, time.time()))
        for method, path in (("get", "/"), ("get", "/entries"), ("get", "/login"), ("post", "/login"),
                             ("post", "/entries"), ("get", "/entries/1")):
            r = getattr(client, method)(path, headers={"HX-Request": "true"} if path == "/entries/1" else None)
            assert r.status_code == 503, (settings, path)
            assert "not set up" in r.text
            assert "fake secret journal line" not in r.text
        assert client.get("/healthz").status_code == 200


def test_env_settings_and_quotes():
    h = TEST_SETTINGS.password_hash.decode()
    s = auth.load_settings({"ANIMUS_PASSWORD_HASH": f"'{h}'", "ANIMUS_SESSION_SECRET": f'"{TEST_SECRET}"'})
    assert s.configured and s.password_hash == h.encode() and s.secret == TEST_SECRET
    assert h not in repr(s) and TEST_SECRET not in repr(s)
    assert not auth.load_settings({}).configured


def test_create_app_reads_env_by_default(db_path, monkeypatch):
    monkeypatch.delenv("ANIMUS_PASSWORD_HASH", raising=False)
    app = create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False)
    assert anon_client(app).get("/").status_code == 503


def test_healthz_open_nothing_leaked(db_path):
    client = anon_client(make_app(db_path))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"
    assert r.headers["content-type"].startswith("text/plain")
    assert "set-cookie" not in r.headers


def test_next_param_open_redirect_blocked(db_path):
    client = anon_client(make_app(db_path))
    for bad in ("//evil.example", "https://evil.example/", "/\\evil.example", "\\\\evil.example",
                "javascript:alert(1)", "evil.example", "/\r\nSet-Cookie:x=1", "/\tx", " /x",
                "///evil.example", "/login", "/logout", "http:/evil.example"):
        assert auth.safe_next(bad) == "/", bad
        r = post_login(client, next_url=bad)
        assert r.status_code == 303 and r.headers["location"] == "/", bad
        client.cookies.clear()
    for good in ("/", "/entries", "/entries?before=2026-11-01T10:00:00Z_5", "/counselor/abc"):
        assert auth.safe_next(good) == good
    page = client.get("/login", params={"next": '/entries"><script>x</script>'})
    assert "<script>x</script>" not in page.text


def test_login_page_ui(db_path):
    client = anon_client(make_app(db_path))
    r = client.get("/login", params={"next": "/memory"})
    assert r.status_code == 200
    html = r.text
    assert '<label class="login-label" for="password">Password</label>' in html
    assert 'type="password"' in html and "autofocus" in html and 'autocomplete="current-password"' in html
    assert 'name="next" value="/memory"' in html
    assert 'href="/entries"' not in html and 'action="/logout"' not in html  # no nav
    assert r.headers["cache-control"] == "no-store"
    # A logged-in owner visiting /login goes straight on.
    logged = login_client(make_app(db_path), follow_redirects=False)
    assert logged.get("/login?next=/memory").headers["location"] == "/memory"


def test_nav_logout_carries_session_csrf(db_path):
    app = make_app(db_path)
    client = login_client(app)
    session = auth.read_session(TEST_SETTINGS, client.cookies[auth.COOKIE_NAME], time.time(),
                               app.state.session_gen.value)
    html = client.get("/").text
    assert html.count('action="/logout"') == 2
    logout_forms = re.findall(r'<form class="logout".*?</form>', html, flags=re.S)
    assert len(logout_forms) == 2
    assert all(f'name="csrf_token" value="{session.csrf}"' in f for f in logout_forms)


def test_five_failures_block_15_minutes_even_with_right_password(db_path, records):
    clock = FakeClock()
    app = make_app(db_path, limiter=LoginLimiter(clock=clock))
    owner = login_client(app)  # a session from before the attack
    client = anon_client(app)
    for _ in range(5):
        assert post_login(client, "wrong wrong").status_code == 401
    r = post_login(client)
    assert r.status_code == 429
    assert r.headers["retry-after"] == "900"
    assert "Try again in 15 minutes" in r.text
    assert "set-cookie" not in r.headers
    clock.advance(899)
    r = post_login(client)
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    assert owner.get("/").status_code == 200  # the lockout never locks out a valid session
    joined = " ".join(records)
    assert "reason=blocked" in joined and "reason=too_many_failures" in joined
    assert TEST_PASSWORD not in joined and "wrong wrong" not in joined


def test_block_expires(db_path):
    clock = FakeClock()
    app = make_app(db_path, limiter=LoginLimiter(clock=clock))
    client = anon_client(app)
    for _ in range(5):
        post_login(client, "wrong wrong")
    clock.advance(900)
    r = post_login(client)
    assert r.status_code == 303 and cookie_header(r)


def test_successful_login_clears_failures(db_path):
    clock = FakeClock()
    limiter = LoginLimiter(clock=clock)
    app = make_app(db_path, limiter=limiter)
    client = anon_client(app)
    for _ in range(4):
        assert post_login(client, "wrong wrong").status_code == 401
    assert post_login(client).status_code == 303
    client.cookies.clear()
    for _ in range(4):
        assert post_login(client, "wrong wrong").status_code == 401
    assert limiter.blocked("testclient") == 0
    assert post_login(client).status_code == 303


def test_forwarded_for_trusted_only_from_loopback(db_path):
    def scope(peer, *xff):
        return {"client": (peer, 1), "headers": [(b"x-forwarded-for", v.encode()) for v in xff]}

    assert auth.client_ip(scope("127.0.0.1", "198.51.100.1, 203.0.113.9")) == "203.0.113.9"
    assert auth.client_ip(scope("127.0.0.1", "198.51.100.1", "203.0.113.10")) == "203.0.113.10"
    assert auth.client_ip(scope("127.0.0.1")) == "127.0.0.1"
    assert auth.client_ip(scope("127.0.0.1", "not-an-ip")) == "127.0.0.1"
    assert auth.client_ip(scope("203.0.113.5", "198.51.100.1")) == "203.0.113.5"
    assert auth.client_ip(scope("::1", "198.51.100.1")) == "::1"

    clock = FakeClock()
    limiter = LoginLimiter(clock=clock)
    app = make_app(db_path, limiter=limiter)
    proxy = anon_client(app, client=LOOPBACK)
    for _ in range(5):
        post_login(proxy, "wrong wrong", headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})
    assert limiter.blocked("198.51.100.7") == 900
    assert post_login(proxy, headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 429
    assert post_login(proxy, headers={"X-Forwarded-For": "198.51.100.7, 198.51.100.8"}).status_code == 303
    # A direct client cannot dodge the limit by inventing X-Forwarded-For values.
    direct = anon_client(app, client=("203.0.113.5", 1))
    for i in range(5):
        post_login(direct, "wrong wrong", headers={"X-Forwarded-For": f"10.0.0.{i}"})
    assert post_login(direct, headers={"X-Forwarded-For": "10.0.0.99"}).status_code == 429


def test_ip_tag_hides_last_octet():
    assert auth.ip_tag("198.51.100.7", "k") == auth.ip_tag("198.51.100.200", "k")
    assert auth.ip_tag("198.51.100.7", "k") != auth.ip_tag("198.51.101.7", "k")
    assert "198" not in auth.ip_tag("198.51.100.7", "k")


def test_logout_post_clears_cookie(db_path):
    app = make_app(db_path)
    client = anon_client(app)
    assert post_login(client).status_code == 303  # the real cookie, as the browser holds it
    assert client.get("/").status_code == 200
    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 403  # a logout needs the token too
    r = client.post("/logout", headers=csrf_headers(app, client))
    assert r.status_code == 303 and r.headers["location"] == "/login"
    (cookie,) = cookie_header(r)
    lowered = cookie.lower()
    assert lowered.startswith(auth.COOKIE_NAME + '=""') or lowered.startswith(auth.COOKIE_NAME + "=;")
    assert "max-age=0" in lowered and "httponly" in lowered
    assert client.get("/").status_code == 303
    # Logging out without a session just goes to the login page.
    assert anon_client(app).post("/logout").headers["location"] == "/login"


def _read_env(path):
    out = {}
    for line in open(path, encoding="utf-8").read().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def test_set_password_script_writes_hash_and_secret_without_printing_them(tmp_path, capsys):
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=fake-key-not-real\n# a comment\nANIMUS_SESSION_" "SECRET=old-secret-old-secret-old-secret-00\n"
                   "ANIMUS_PASSWORD_" "HASH=stale\nLLM_MODEL=fake/model\n", encoding="utf-8")
    os.chmod(env, 0o600)
    password = "a brand new fake passphrase"
    prompts, said = [], []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return password

    assert set_password.main(str(env), getpass=fake_getpass, out=said.append, cost=4) == 0
    assert said == ["Password saved."]
    assert len(prompts) == 2
    text = env.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[:3] == ["LLM_API_KEY=fake-key-not-real", "# a comment", "LLM_MODEL=fake/model"]
    values = _read_env(env)
    settings = auth.load_settings(values)
    assert settings.configured
    assert auth.verify_password(settings, password)
    assert settings.secret != "old-secret-old-secret-old-secret-00"
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    captured = capsys.readouterr()
    for secret_value in (settings.password_hash.decode(), settings.secret, password):
        assert secret_value not in captured.out + captured.err + " ".join(said)
    assert text.count("ANIMUS_PASSWORD_HASH=") == 1 and text.count("ANIMUS_SESSION_SECRET=") == 1
    # Changing the password rotates the secret, so every old session ends.
    first_secret = settings.secret
    assert set_password.main(str(env), getpass=lambda p: "another fake passphrase", out=said.append, cost=4) == 0
    assert auth.load_settings(_read_env(env)).secret != first_secret
    # Too short or mismatched: nothing changes.
    before = env.read_text(encoding="utf-8")
    said.clear()
    assert set_password.main(str(env), getpass=lambda p: "short", out=said.append, cost=4) == 1
    answers = iter(["first fake passphrase", "second fake passphrase"])
    assert set_password.main(str(env), getpass=lambda p: next(answers), out=said.append, cost=4) == 1
    assert env.read_text(encoding="utf-8") == before
    assert all("Nothing changed" in s for s in said)
    assert list(tmp_path.glob(".env.*.tmp")) == []
