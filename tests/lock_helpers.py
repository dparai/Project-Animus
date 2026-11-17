"""Helpers for the app-lock tests: an app with a clock the test moves, and real logins."""
from app import auth
from app.limits import LoginLimiter
from app.main import create_app
from tests.auth_helpers import TEST_PASSWORD, TEST_SETTINGS, anon_client, csrf_headers

START = 1_800_000_000.0
ANDROID = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class Clock:
    def __init__(self, now: float = START):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def make_app(tmp_path, clock=None, name="lock.db"):
    clock = clock or Clock()
    app = create_app(str(tmp_path / name), allowed_hosts=["testserver"], start_scheduler=False,
                     auth=TEST_SETTINGS, limiter=LoginLimiter(clock=clock), clock=clock)
    return app, clock


def login(app, user_agent: str = ANDROID, password: str = TEST_PASSWORD):
    """A client that logged in through the real form (so it has a device). CSRF header included."""
    client = anon_client(app, headers={"User-Agent": user_agent})
    r = client.post("/login", data={"password": password})
    assert r.status_code == 303, r.status_code
    client.headers.update(csrf_headers(app, client))
    return client


def refresh_csrf(app, client) -> None:
    client.headers.update(csrf_headers(app, client))


def device_id(app, client) -> str:
    session = auth.read_session(app.state.auth, client.cookies.get(auth.COOKIE_NAME), app.state.clock(),
                                app.state.session_gen.value)
    assert session is not None and session.device
    return session.device


def set_lock(client, mode: str, after: int = 60):
    r = client.post("/settings/lock", data={"mode": mode, "after": str(after)})
    assert r.status_code == 303 and r.headers["location"] == "/settings?done=lock", r.headers.get("location")
    return r
