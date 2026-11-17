"""App lock per device: the server refuses a locked device, whatever the page does."""
import sqlite3

from app import auth, devices
from tests.auth_helpers import TEST_PASSWORD, anon_client, login_client
from tests.lock_helpers import ANDROID, WINDOWS, device_id, login, make_app, refresh_csrf, set_lock

HX = {"HX-Request": "true"}


def rows(app, sql, args=()):
    conn = sqlite3.connect(app.state.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def set_cookies(response):
    return [v for k, v in response.headers.multi_items() if k == "set-cookie"]


def test_login_records_a_device_and_sets_a_device_cookie(tmp_path):
    app, _ = make_app(tmp_path)
    client = anon_client(app, headers={"User-Agent": ANDROID})
    r = client.post("/login", data={"password": TEST_PASSWORD})
    session_cookie, device_cookie = set_cookies(r)
    assert session_cookie.startswith(auth.COOKIE_NAME + "=") and "Max-Age=86400" in session_cookie
    flags = [p.strip().lower() for p in device_cookie.split(";")]
    assert flags[0].startswith(devices.COOKIE_NAME + "=") and "httponly" in flags and "samesite=lax" in flags
    (row,) = rows(app, "SELECT * FROM devices")
    assert row["label"] == "Chrome on Android" and row["lock_mode"] == "none" and not row["revoked"]
    assert ANDROID not in str(dict(row))  # the raw User-Agent is not stored
    page = client.get("/settings")
    assert page.status_code == 200 and "This device" in page.text and "Chrome on Android" in page.text


def test_device_and_its_lock_survive_logout_and_login(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    first = device_id(app, client)
    set_lock(client, "password", 300)
    refresh_csrf(app, client)
    assert client.post("/logout").status_code == 303
    assert client.get("/").status_code == 303  # logged out
    r = client.post("/login", data={"password": TEST_PASSWORD})
    assert r.status_code == 303
    assert device_id(app, client) == first
    (row,) = rows(app, "SELECT * FROM devices")
    assert row["lock_mode"] == "password" and row["lock_after_s"] == 300 and row["locked"] == 0
    assert "Max-Age=2592000" in set_cookies(r)[0]  # a locked device keeps its long session


def test_a_forged_or_foreign_device_cookie_gets_a_new_device(tmp_path):
    app, _ = make_app(tmp_path)
    client = anon_client(app)
    client.cookies.set(devices.COOKIE_NAME, "not-a-signed-value")
    assert client.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    other = anon_client(app)
    other.cookies.set(devices.COOKIE_NAME, devices.sign_id("another-secret-another-secret-0123456789", "A" * 24))
    assert other.post("/login", data={"password": TEST_PASSWORD}).status_code == 303
    assert len(rows(app, "SELECT id FROM devices")) == 2
    assert devices.read_id("s" * 40, "x" * 600) is None and devices.read_id(None, "x") is None


def test_changing_the_lock_needs_the_password_again(tmp_path):
    app, clock = make_app(tmp_path)
    client = login(app)
    clock.tick(devices.SUDO_S + 1)  # the login's five minutes are over
    r = client.post("/settings/lock", data={"mode": "password", "after": "60"})
    assert r.headers["location"] == "/settings?problem=confirm"
    assert rows(app, "SELECT lock_mode FROM devices")[0]["lock_mode"] == "none"
    assert "Enter your password to change the lock" in client.get("/settings").text
    r = client.post("/settings/confirm", data={"password": "wrong password"})
    assert r.headers["location"] == "/settings?problem=password"
    assert client.post("/settings/lock", data={"mode": "password", "after": "60"}).headers["location"].endswith("confirm")
    assert client.post("/settings/confirm", data={"password": TEST_PASSWORD}).headers["location"] == "/settings"
    set_lock(client, "password", 60)
    for bad in ({"mode": "biometric", "after": "60"}, {"mode": "password", "after": "7"}, {"mode": "password", "after": "x"}):
        assert client.post("/settings/lock", data=bad).status_code == 422
    # fingerprint cannot be chosen before a passkey exists
    assert client.post("/settings/lock", data={"mode": "passkey", "after": "60"}).headers["location"].endswith("nopasskey")


def test_locked_device_gets_nothing_until_unlocked(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    set_lock(client, "password", 60)
    refresh_csrf(app, client)
    assert client.post("/entries", data={"text": "fake entry before the lock"}).status_code == 200
    assert client.post("/lock").status_code == 204

    page = client.get("/entries?day=2026-01-01")
    assert page.status_code == 303 and page.headers["location"] == "/locked?next=%2Fentries%3Fday%3D2026-01-01"
    assert client.get("/entries", headers=HX).status_code == 423
    assert client.get("/theme-state", headers={"Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"}).status_code == 423
    refused = client.post("/entries", data={"text": "fake entry while locked"})
    assert refused.status_code == 423 and refused.headers["x-animus-locked"] == "1" and refused.text == ""
    assert client.post("/lock/ping").status_code == 423
    assert client.post("/settings/lock", data={"mode": "none", "after": "60"}).status_code == 423
    assert len(rows(app, "SELECT id FROM entries")) == 1
    assert client.get("/static/theme.css").status_code == 200  # public files only

    lock_page = client.get("/locked?next=/entries")
    assert lock_page.status_code == 200 and 'action="/unlock"' in lock_page.text
    assert "fake entry" not in lock_page.text and 'name="next" value="/entries"' in lock_page.text
    assert lock_page.headers["cache-control"] == "no-store"

    wrong = client.post("/unlock", data={"password": "wrong password", "next": "/entries"})
    assert wrong.status_code == 401 and "That didn&#39;t work." in wrong.text
    assert client.get("/").status_code == 303
    right = client.post("/unlock", data={"password": TEST_PASSWORD, "next": "/entries"})
    assert right.status_code == 303 and right.headers["location"] == "/entries"
    assert "Max-Age=2592000" in set_cookies(right)[0]
    assert client.get("/").status_code == 200
    assert client.get("/locked?next=/ask").headers["location"] == "/ask"  # nothing to unlock


def test_unlock_from_the_open_page_answers_json_and_keeps_the_csrf_token(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    set_lock(client, "password", 0)
    refresh_csrf(app, client)
    token = client.headers["X-CSRF-Token"]
    client.post("/lock")
    fetch = {"X-Requested-With": "fetch"}
    bad = client.post("/unlock", data={"password": "nope"}, headers=fetch)
    assert bad.status_code == 401 and bad.json() == {"ok": False, "message": "That didn't work."}
    good = client.post("/unlock", data={"password": TEST_PASSWORD}, headers=fetch)
    assert good.status_code == 200 and good.json() == {"ok": True}
    # the page that was open keeps working: same token, a save goes through
    assert client.post("/entries", data={"text": "fake text typed before the lock"}, headers={"X-CSRF-Token": token}).status_code == 200


def test_unlock_needs_the_csrf_token_and_is_rate_limited(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    set_lock(client, "password", 60)
    refresh_csrf(app, client)
    client.post("/lock")
    token = client.headers.pop("X-CSRF-Token")
    assert client.post("/unlock", data={"password": TEST_PASSWORD}).status_code == 403
    client.headers["X-CSRF-Token"] = token
    for _ in range(5):
        assert client.post("/unlock", data={"password": "wrong password"}).status_code == 401
    blocked = client.post("/unlock", data={"password": TEST_PASSWORD})
    assert blocked.status_code == 429 and "Too many tries" in blocked.text
    assert client.get("/").status_code == 303  # still locked


def test_timer_locks_when_the_page_stops_pinging(tmp_path):
    app, clock = make_app(tmp_path)
    client = login(app)
    set_lock(client, "password", 60)
    refresh_csrf(app, client)
    for _ in range(20):  # ten minutes of a visible page
        clock.tick(devices.PING_S)
        assert client.post("/lock/ping").status_code == 204
    assert client.get("/").status_code == 200
    clock.tick(60 + devices.SLACK_S - 1)
    assert client.get("/").status_code == 200  # still inside the timer
    clock.tick(2)
    assert client.get("/").status_code == 303
    clock.tick(-120)  # even if the clock looked earlier again, the lock was written down
    assert client.post("/lock/ping").status_code == 423
    assert client.get("/").headers["location"] == "/locked"


def test_other_requests_do_not_keep_a_device_awake(tmp_path):
    app, clock = make_app(tmp_path)
    client = login(app)
    set_lock(client, "password", 60)
    refresh_csrf(app, client)
    for _ in range(6):  # a page left polling in the background is not "in use"
        clock.tick(30)
        client.get("/theme-state")
    assert client.get("/").status_code == 303


def test_long_session_only_while_the_lock_is_on(tmp_path):
    app, clock = make_app(tmp_path)
    client = login(app)
    set_lock(client, "password", 60)
    refresh_csrf(app, client)
    long_cookie = client.cookies.get(auth.COOKIE_NAME)
    clock.tick(3 * 86400)
    assert client.get("/").headers["location"] == "/locked"  # still logged in, just locked
    assert client.post("/unlock", data={"password": TEST_PASSWORD}).status_code == 303
    refresh_csrf(app, client)
    assert client.post("/settings/confirm", data={"password": TEST_PASSWORD}).status_code == 303
    off = client.post("/settings/lock", data={"mode": "none", "after": "60"})
    assert "Max-Age=86400" in set_cookies(off)[0]
    # the old 30-day cookie must not outlive the lock it was issued for
    clock.tick(2 * 86400)
    stale = anon_client(app)
    stale.cookies.set(auth.COOKIE_NAME, long_cookie)
    assert stale.get("/").headers["location"] == "/login"
    clock.tick(31 * 86400)
    assert client.get("/").headers["location"] == "/login"


def test_session_lifetime_cannot_be_raised_or_malformed(tmp_path):
    app, clock = make_app(tmp_path)
    settings, gen = app.state.auth, app.state.session_gen.value
    huge = auth.issue_session(settings, clock(), gen, lifetime_s=10 ** 9)
    assert auth.read_session(settings, huge, clock(), gen).lifetime_s == auth.MAX_LIFETIME_S
    short = auth.issue_session(settings, clock(), gen, device="d" * 24, lifetime_s=100)
    assert auth.read_session(settings, short, clock() + 100, gen).device == "d" * 24
    assert auth.read_session(settings, short, clock() + 101, gen) is None
    plain = auth.issue_session(settings, clock(), gen)
    assert auth.read_session(settings, plain, clock() + 86400, gen) is not None
    assert auth.read_session(settings, plain, clock() + 86401, gen) is None
    # a session naming a device that does not exist is no session at all
    ghost = anon_client(app)
    ghost.cookies.set(auth.COOKIE_NAME, auth.issue_session(settings, clock(), gen, device="g" * 24))
    assert ghost.get("/").headers["location"] == "/login"


def test_sign_out_one_device(tmp_path):
    app, _ = make_app(tmp_path)
    phone, laptop = login(app, ANDROID), login(app, WINDOWS)
    page = phone.get("/settings").text
    assert "Chrome on Windows" in page and page.count("Sign out</button>") == 1
    r = phone.post(f"/settings/devices/{device_id(app, laptop)}/signout")
    assert r.headers["location"] == "/settings?done=device"
    assert laptop.get("/").headers["location"] == "/login"
    assert phone.get("/").status_code == 200
    assert "Logged out" in phone.get("/settings").text
    # signing out the device in hand goes to the login page
    r = phone.post(f"/settings/devices/{device_id(app, phone)}/signout")
    assert r.headers["location"] == "/login" and phone.get("/").headers["location"] == "/login"


def test_log_out_signs_out_every_device(tmp_path):
    app, _ = make_app(tmp_path)
    phone, laptop = login(app, ANDROID), login(app, WINDOWS)
    assert phone.post("/logout").status_code == 303
    assert laptop.get("/").headers["location"] == "/login"
    again = login(app, WINDOWS)
    assert again.get("/settings").text.count("Logged out") == 2  # the two old ones; this login is a third


def test_locked_device_can_still_log_out(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    set_lock(client, "password", 60)
    refresh_csrf(app, client)
    client.post("/lock")
    assert client.post("/logout").headers["location"] == "/login"
    assert client.get("/").headers["location"] == "/login"


def test_session_without_a_device_has_no_lock_controls(tmp_path):
    app, _ = make_app(tmp_path)
    client = login_client(app, follow_redirects=False)  # a cookie from before devices existed
    page = client.get("/settings")
    assert page.status_code == 200 and "Log out and log in again on this device" in page.text
    assert client.post("/settings/lock", data={"mode": "password", "after": "60"}).headers["location"].endswith("nodevice")
    assert client.post("/lock").status_code == 204 and client.post("/lock/ping").status_code == 204
    assert client.get("/").status_code == 200
    assert 'id="lock"' not in client.get("/").text


def test_pages_of_a_locking_device_carry_the_unlock_sheet(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    assert 'id="lock"' not in client.get("/").text
    set_lock(client, "password", 300)
    page = client.get("/").text
    assert 'id="lock" class="lock" hidden' in page and 'data-after="300"' in page and "/static/lock.js" in page
    assert 'href="/settings"' in page


def test_lock_paths_are_never_a_redirect_target():
    assert auth.safe_next("/locked") == "/" and auth.safe_next("/locked?next=/x") == "/"
    assert auth.safe_next("/settings") == "/settings"


def test_device_labels_and_table_stays_small(tmp_path):
    assert devices.label_for("") == "Unknown device"
    assert devices.label_for("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Version/17.0 Mobile/15E148 Safari/604.1") == "Safari on iPhone"
    assert devices.label_for("Mozilla/5.0 (Windows NT 10.0) Chrome/130.0 Safari/537.36 Edg/130.0") == "Edge on Windows"
    app, _ = make_app(tmp_path)
    conn = sqlite3.connect(app.state.db_path)
    conn.row_factory = sqlite3.Row
    for i in range(devices.MAX_DEVICES + 5):
        devices.attach(conn, None, "", 1000 + i, gen=i)  # each login under an older generation
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] <= devices.MAX_DEVICES
    conn.close()
