"""Fake login settings for tests and a helper that builds an already-logged-in client."""
from fastapi.testclient import TestClient

from app import auth

TEST_PASSWORD = "fake test password 123"
TEST_SECRET = "fake-session-secret-for-tests-only-0123456789"
TEST_HASH = auth.hash_password(TEST_PASSWORD, cost=4)  # cost 4 keeps the suite fast
TEST_SETTINGS = auth.AuthSettings(password_hash=TEST_HASH, secret=TEST_SECRET)


def login_client(app, **kwargs) -> TestClient:
    """A TestClient carrying a valid session cookie for the app's own auth settings.

    Its default headers include X-CSRF-Token for that session, so unsafe requests pass the CSRF
    check (pop it, or use csrf_headers, to test the token itself).
    """
    client = TestClient(app, **kwargs)
    cookie = auth.issue_session(app.state.auth, app.state.clock(), app.state.session_gen.value)
    client.cookies.set(auth.COOKIE_NAME, cookie)
    client.headers["X-CSRF-Token"] = session_token(app, cookie)
    return client


def session_token(app, cookie: str) -> str:
    """The CSRF token carried inside a session cookie value."""
    session = auth.read_session(app.state.auth, cookie, app.state.clock(), app.state.session_gen.value)
    assert session is not None, "cookie is not a valid session"
    return session.csrf


def csrf_headers(app, client) -> dict:
    """{'X-CSRF-Token': ...} for the session cookie the client holds now (e.g. after a real login)."""
    return {"X-CSRF-Token": session_token(app, client.cookies.get(auth.COOKIE_NAME))}


def anon_client(app, **kwargs) -> TestClient:
    """A TestClient with no session (redirects are not followed, so they can be asserted)."""
    kwargs.setdefault("follow_redirects", False)
    return TestClient(app, **kwargs)
