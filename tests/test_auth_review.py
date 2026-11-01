"""Review fixes: lockout race, server-side logout, bounded login body, script hardening."""
import asyncio
import io
import sys
import time

import httpx

from app import auth, db
from app.limits import LoginLimiter
from app.main import create_app
from scripts import set_password
from tests.auth_helpers import TEST_PASSWORD, TEST_SETTINGS, anon_client, csrf_headers, login_client


def make_app(db_path, **kwargs):
    return create_app(db_path, allowed_hosts=["testserver"], start_scheduler=False, auth=TEST_SETTINGS, **kwargs)


def test_parallel_burst_cannot_beat_the_lockout(tmp_path, monkeypatch):
    limiter = LoginLimiter()
    app = make_app(str(tmp_path / "race.db"), limiter=limiter)
    calls = []
    real = auth.verify_password

    def counting(settings, password):
        calls.append(1)
        time.sleep(0.05)  # keep checks in flight while the burst arrives
        return real(settings, password)

    monkeypatch.setattr(auth, "verify_password", counting)
    ip = "203.0.113.9"

    async def burst():
        transport = httpx.ASGITransport(app=app, client=(ip, 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            wrong = [client.post("/login", data={"password": "wrong wrong"}) for _ in range(10)]
            right = client.post("/login", data={"password": TEST_PASSWORD})
            return await asyncio.gather(*wrong, right)

    responses = asyncio.run(burst())
    assert responses[-1].status_code == 429
    assert "set-cookie" not in responses[-1].headers
    assert len(calls) <= 5
    assert sum(r.status_code == 401 for r in responses) <= 5
    assert all(r.status_code in (401, 429) for r in responses)
    assert limiter.blocked(ip) > 0
    # The block stays, even for the right password afterwards.
    after = anon_client(app, client=(ip, 1))
    assert after.post("/login", data={"password": TEST_PASSWORD}).status_code == 429
    assert limiter.blocked(ip) > 0


def test_right_password_checked_while_block_lands_gets_no_cookie(tmp_path, monkeypatch):
    limiter = LoginLimiter()
    app = make_app(str(tmp_path / "late.db"), limiter=limiter)

    def blocked_meanwhile(settings, password):
        for _ in range(5):  # parallel failures land while this check runs
            limiter.fail("testclient")
        return True

    monkeypatch.setattr(auth, "verify_password", blocked_meanwhile)
    r = anon_client(app).post("/login", data={"password": TEST_PASSWORD})
    assert r.status_code == 429
    assert "set-cookie" not in r.headers
    assert limiter.blocked("testclient") == 900


def test_copied_cookie_rejected_after_logout(tmp_path):
    app = make_app(str(tmp_path / "gen.db"))
    owner = anon_client(app)
    assert owner.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    copied = owner.cookies[auth.COOKIE_NAME]
    thief = anon_client(app)
    thief.cookies.set(auth.COOKIE_NAME, copied)
    assert thief.get("/").status_code == 200
    assert owner.post("/logout", headers=csrf_headers(app, owner)).status_code == 303
    r = thief.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # Logging in again works.
    again = anon_client(app)
    assert again.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    assert again.get("/").status_code == 200
    # Old-format cookies without a generation are refused.
    no_gen = auth._serializer(TEST_SETTINGS.secret, time.time()).dumps({"c": "x" * 43})
    assert auth.read_session(TEST_SETTINGS, no_gen, time.time()) is None


def test_session_generation_survives_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    app = make_app(path)
    start = app.state.session_gen.value
    old_cookie = auth.issue_session(TEST_SETTINGS, time.time(), start)
    client = login_client(app, follow_redirects=False)
    assert client.post("/logout").status_code == 303
    assert app.state.session_gen.value == start + 1
    c = db.connect(path)
    assert c.execute("SELECT value FROM settings WHERE key = 'session_gen'").fetchone()[0] == str(start + 1)
    c.close()
    restarted = make_app(path)
    assert restarted.state.session_gen.value == start + 1
    stale = anon_client(restarted)
    stale.cookies.set(auth.COOKIE_NAME, old_cookie)
    assert stale.get("/").status_code == 303
    fresh = anon_client(restarted)
    assert fresh.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    assert fresh.get("/").status_code == 200


def test_login_body_over_8kb_refused_before_parsing(tmp_path, monkeypatch):
    app = make_app(str(tmp_path / "body.db"))
    calls = []
    monkeypatch.setattr(auth, "verify_password", lambda s, p: calls.append(1) or False)
    client = anon_client(app)
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    big = b"password=" + b"a" * 1_000_000
    r = client.post("/login", content=big, headers=form)
    assert r.status_code == 413
    streamed = client.post("/login", content=iter([b"password=", b"a" * 5000, b"b" * 5000]), headers=form)
    assert streamed.status_code == 413
    assert calls == []
    assert app.state.limiter.blocked("testclient") == 0
    # A normal-size login still works.
    monkeypatch.setattr(auth, "verify_password", lambda s, p: p == TEST_PASSWORD)
    assert client.post("/login", data={"password": TEST_PASSWORD}).status_code == 303


def test_set_password_refuses_without_a_terminal(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped password\npiped password\n"))
    said = []
    assert set_password.main(str(env), out=said.append, cost=4) == 1
    assert said == ["Run this in a real terminal."]
    assert env.read_text(encoding="utf-8") == "OTHER=1\n"


def test_logout_fails_closed_when_database_write_fails(tmp_path, caplog):
    path = str(tmp_path / "failclosed.db")
    app = make_app(path)
    owner = anon_client(app)
    assert owner.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    cookie = owner.cookies[auth.COOKIE_NAME]
    c = db.connect(path)
    c.execute("DROP TABLE settings")  # every generation write now fails
    c.commit()
    c.close()
    before = app.state.session_gen.value
    r = owner.post("/logout", headers=csrf_headers(app, owner))
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "animus_session" in r.headers.get("set-cookie", "")
    assert app.state.session_gen.value == before + 1
    assert "OperationalError" in caplog.text
    stale = anon_client(app)
    stale.cookies.set(auth.COOKIE_NAME, cookie)
    assert stale.get("/").status_code == 303


def test_logout_survives_unopenable_database(tmp_path, monkeypatch):
    app = make_app(str(tmp_path / "noopen.db"))
    owner = anon_client(app)
    assert owner.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    cookie = owner.cookies[auth.COOKIE_NAME]
    headers = csrf_headers(app, owner)

    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(db, "connect", boom)
    r = owner.post("/logout", headers=headers)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    stale = anon_client(app)
    stale.cookies.set(auth.COOKIE_NAME, cookie)
    assert stale.get("/").status_code == 303


def test_corrupt_generation_row_does_not_revive_old_cookie(tmp_path, caplog):
    path = str(tmp_path / "corrupt.db")
    boot = db.connect(path)
    db.init_db(boot)
    boot.execute("INSERT INTO settings(key, value) VALUES ('session_gen', '0')")
    boot.commit()
    boot.close()
    app = make_app(path)
    assert app.state.session_gen.value == 0
    owner = anon_client(app)
    assert owner.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    cookie = owner.cookies[auth.COOKIE_NAME]
    assert owner.post("/logout", headers=csrf_headers(app, owner)).status_code == 303
    c = db.connect(path)
    c.execute("UPDATE settings SET value = 'abc' WHERE key = 'session_gen'")
    c.commit()
    c.close()
    restarted = make_app(path)
    assert restarted.state.session_gen.value >= int(time.time()) - 5
    assert "abc" not in caplog.text
    c = db.connect(path)
    stored = int(c.execute("SELECT value FROM settings WHERE key = 'session_gen'").fetchone()[0])
    c.close()
    assert stored == restarted.state.session_gen.value
    stale = anon_client(restarted)
    stale.cookies.set(auth.COOKIE_NAME, cookie)
    assert stale.get("/").status_code == 303


def test_missing_generation_row_starts_from_clock_and_bump_takes_max(tmp_path):
    boot = db.connect(str(tmp_path / "missing.db"))
    db.init_db(boot)
    gen = auth.SessionGeneration.load(boot)
    assert gen.value >= int(time.time()) - 5
    boot.execute("UPDATE settings SET value = '5000000000' WHERE key = 'session_gen'")
    boot.commit()
    assert gen.bump(boot) == 5000000001  # max(in-memory, stored) + 1
    boot.close()


def test_cancelled_after_wrong_password_still_counts_the_failure(tmp_path, monkeypatch):
    import app.main as main_mod

    limiter = LoginLimiter()
    app = make_app(str(tmp_path / "cancel.db"), limiter=limiter)

    async def finished_then_cancelled(func, *args):
        func(*args)  # the check completed (wrong password) ...
        raise asyncio.CancelledError()  # ... and then the handler was cancelled

    monkeypatch.setattr(main_mod, "run_in_threadpool", finished_then_cancelled)
    ip = "203.0.113.77"

    async def go():
        transport = httpx.ASGITransport(app=app, client=(ip, 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            try:
                await client.post("/login", data={"password": "wrong wrong"})
            except BaseException:  # noqa: BLE001
                pass

    asyncio.run(go())
    assert len(limiter._recent(ip, limiter.clock())) == 1
    assert limiter._in_flight.get(ip, 0) == 0
