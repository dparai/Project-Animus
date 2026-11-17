"""Daily reminder: what is sent, where it may be sent, and when."""
import base64
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402

from app import db, push  # noqa: E402
from scripts import make_push_key  # noqa: E402
from tests.auth_helpers import login_client  # noqa: E402
from tests.lock_helpers import login, make_app  # noqa: E402

FCM = "https://fcm.googleapis.com/fcm/send/fake-subscription-id"
APPLE = "https://web.push.apple.com/fake-subscription-id"
JSON = {"X-Requested-With": "fetch"}
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@pytest.fixture
def vapid():
    return push.load_vapid({"ANIMUS_VAPID_PRIVATE_KEY": make_push_key.new_key(), "ANIMUS_ALLOWED_HOSTS": "journal.example.com"})


class Service:
    """A fake push service: records requests, answers with the status set on it."""

    def __init__(self, status=201):
        self.status, self.requests = status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    @property
    def transport(self):
        return httpx.MockTransport(self)


@pytest.mark.parametrize("url,ok", [
    (FCM, True), (APPLE, True),
    ("https://updates.push.services.mozilla.com/wpush/v2/fake", True),
    ("https://db5p.notify.windows.com/w/?token=fake", True),
    ("http://fcm.googleapis.com/fcm/send/x", False),
    ("https://fcm.googleapis.com.evil.example/x", False),
    ("https://evilfcm.googleapis.com.example/x", False),
    ("https://notfcm.googleapis.com/x", False),  # another host of the same company is not enough
    ("https://127.0.0.1/x", False), ("https://localhost:8100/x", False),
    ("https://user:pw@fcm.googleapis.com/x", False), ("https://fcm.googleapis.com:8443/x", False),
    ("https://fcm.googleapis.com/x y", False), ("", False), (None, False), (5, False),
    ("https://fcm.googleapis.com/" + "a" * 1000, False),
])
def test_only_known_push_services(url, ok):
    assert push.endpoint_ok(url) is ok


def test_key_loading(vapid):
    assert vapid is not None and len(unb64(vapid.public_key)) == 65 and vapid.subject == "https://journal.example.com"
    assert "private_key" not in repr(vapid)
    assert push.load_vapid({}) is None
    assert push.load_vapid({"ANIMUS_VAPID_PRIVATE_KEY": "not base64 !!"}) is None
    assert push.load_vapid({"ANIMUS_VAPID_PRIVATE_KEY": "c2hvcnQ"}) is None
    custom = push.load_vapid({"ANIMUS_VAPID_PRIVATE_KEY": make_push_key.new_key(), "ANIMUS_VAPID_SUBJECT": "mailto:fake@example.com"})
    assert custom.subject == "mailto:fake@example.com"


def test_authorization_is_a_valid_token_for_that_service_only(vapid):
    header = push.authorization(vapid, FCM, 1_800_000_000)
    assert header.startswith("vapid t=") and header.endswith(", k=" + vapid.public_key)
    token = header[len("vapid t="):].split(",")[0]
    head, body, signature = token.split(".")
    assert json.loads(unb64(head)) == {"typ": "JWT", "alg": "ES256"}
    claims = json.loads(unb64(body))
    assert claims == {"aud": "https://fcm.googleapis.com", "exp": 1_800_000_000 + push.TOKEN_S, "sub": "https://journal.example.com"}
    raw = unb64(signature)
    assert len(raw) == 64
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64(vapid.public_key))
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if wrong


def test_a_push_carries_no_content(vapid):
    service = Service()
    assert push.send(vapid, FCM, 1_800_000_000, service.transport) == "ok"
    (request,) = service.requests
    assert request.method == "POST" and str(request.url) == FCM and request.content == b""
    assert request.headers["ttl"] == str(push.TTL_S) and request.headers["authorization"].startswith("vapid t=")
    assert "content-encoding" not in request.headers
    assert push.send(vapid, FCM, 1.0, Service(410).transport) == "gone"
    assert push.send(vapid, FCM, 1.0, Service(404).transport) == "gone"
    assert push.send(vapid, FCM, 1.0, Service(500).transport) == "error"
    assert push.send(vapid, FCM, 1.0, Service(301).transport) == "error"  # redirects are not followed
    unreachable = Service()
    assert push.send(vapid, "https://evil.example/x", 1.0, unreachable.transport) == "gone" and not unreachable.requests

    def down(request):
        raise httpx.ConnectError("no route")

    assert push.send(vapid, FCM, 1.0, httpx.MockTransport(down)) == "error"


def test_dead_subscriptions_are_dropped(conn, vapid):
    push.subscribe(conn, "dev-a", FCM, 100)
    push.subscribe(conn, "dev-b", APPLE, 100)
    assert push.send_all(conn, vapid, 200.0, Service(410).transport) == 0
    assert push.subscription_count(conn) == 0
    push.subscribe(conn, "dev-a", FCM, 100)
    for _ in range(push.MAX_FAILURES - 1):
        assert push.send_all(conn, vapid, 200.0, Service(500).transport) == 0
    assert push.subscription_count(conn) == 1
    push.send_all(conn, vapid, 200.0, Service(500).transport)
    assert push.subscription_count(conn) == 0


def test_one_subscription_per_device_and_a_bounded_table(conn):
    assert push.subscribe(conn, "dev-a", FCM, 100) and push.subscribe(conn, "dev-a", FCM + "-new", 101)
    assert push.subscription_count(conn) == 1 and push.device_subscribed(conn, "dev-a")
    assert not push.subscribe(conn, "dev-a", "https://evil.example/x", 102)
    for i in range(push.MAX_SUBSCRIPTIONS + 5):
        push.subscribe(conn, f"dev-{i}", f"{FCM}-{i}", 200 + i)
    assert push.subscription_count(conn) == push.MAX_SUBSCRIPTIONS
    push.unsubscribe(conn, "dev-10")
    assert not push.device_subscribed(conn, "dev-10") and not push.device_subscribed(conn, None)


def at(hour, minute=0, day=10):
    return datetime(2026, 3, day, hour, minute, tzinfo=timezone.utc)


def test_reminder_is_due_once_after_the_chosen_time(conn):
    conn.execute("INSERT INTO settings(key, value) VALUES ('timezone', 'UTC')")
    assert push.get_reminder(conn) == (False, push.DEFAULT_TIME)
    assert push.reminder_due(conn, at(22)) is None  # off
    push.set_reminder(conn, True, "21:00")
    assert push.reminder_due(conn, at(20, 59)) is None
    assert push.reminder_due(conn, at(21)) == "2026-03-10"
    assert push.reminder_due(conn, at(23, 55)) == "2026-03-10"
    assert push.reminder_due(conn, at(3, 30, day=11)) is None  # more than six hours late: skipped
    conn.execute("INSERT INTO entries(created_at, local_date, text) VALUES ('2026-03-10T10:00:00Z', '2026-03-10', 'fake entry')")
    assert push.reminder_due(conn, at(21, 5)) is None  # already wrote today
    assert push.reminder_due(conn, at(21, 5, day=11)) == "2026-03-11"
    for bad in ("9:00", "24:00", "21:60", "", "21:00:00", "ab:cd"):
        with pytest.raises(ValueError):
            push.set_reminder(conn, True, bad)


def test_reminder_follows_the_stored_timezone(conn):
    conn.execute("INSERT INTO settings(key, value) VALUES ('timezone', 'Asia/Kolkata')")
    push.set_reminder(conn, True, "21:00")
    assert push.reminder_due(conn, at(15, 29)) is None  # 20:59 in Kolkata
    assert push.reminder_due(conn, at(15, 30)) == "2026-03-10"
    assert push.reminder_due(conn, at(19)) is None  # 00:30 the next day there: a new day, not yet time


def test_reminder_tick_sends_once_a_day(tmp_path, vapid):
    path = str(tmp_path / "tick.db")
    c = db.connect(path)
    db.init_db(c)
    c.execute("INSERT INTO settings(key, value) VALUES ('timezone', 'UTC')")
    push.set_reminder(c, True, "21:00")
    service = Service()
    assert push.reminder_tick(path, at(21, 1), vapid, service.transport) == 0  # nobody subscribed: not used up
    push.subscribe(c, "dev-a", FCM, 100)
    push.subscribe(c, "dev-b", APPLE, 100)
    assert push.reminder_tick(path, at(20), vapid, service.transport) == 0
    assert push.reminder_tick(path, at(21, 5), vapid, service.transport) == 2
    assert push.reminder_tick(path, at(21, 10), vapid, service.transport) == 0
    assert len(service.requests) == 2 and all(r.content == b"" for r in service.requests)
    assert push.reminder_tick(path, at(21, 5, day=11), vapid, service.transport) == 2
    assert push.reminder_tick(path, at(21, 5, day=12), None, service.transport) == 0  # no key on the server (env is empty)
    c.close()


def test_subscribe_routes(tmp_path, vapid):
    app, _ = make_app(tmp_path)
    client = login(app)
    assert client.post("/push/subscribe", json={"endpoint": FCM}, headers=JSON).status_code == 422  # no key yet
    assert "Reminders are not set up on the server yet." in client.get("/settings").text
    app.state.vapid = vapid
    page = client.get("/settings").text
    assert f'data-push-key="{vapid.public_key}"' in page and 'id="push-on"' in page
    for bad in ("https://evil.example/hook", "http://127.0.0.1:4533/", None, {"x": 1}):
        assert client.post("/push/subscribe", json={"endpoint": bad}, headers=JSON).status_code == 422
    assert client.post("/push/subscribe", content=b"not json", headers={**JSON, "Content-Type": "application/json"}).status_code == 422
    assert client.post("/push/subscribe", json={"endpoint": FCM}, headers=JSON).status_code == 204
    assert 'data-subscribed="1"' in client.get("/settings").text
    service = Service()
    app.state.push_transport = service.transport
    assert client.post("/push/test", json={}, headers=JSON).json() == {"sent": 1}
    assert str(service.requests[0].url) == FCM and service.requests[0].content == b""
    assert client.post("/push/unsubscribe", json={}, headers=JSON).status_code == 204
    assert client.post("/push/test", json={}, headers=JSON).json() == {"sent": 0}
    # a session without a device cannot subscribe
    old = login_client(app)
    assert old.post("/push/subscribe", json={"endpoint": FCM}, headers=JSON).status_code == 422


def test_push_routes_need_the_csrf_token(tmp_path, vapid):
    app, _ = make_app(tmp_path)
    app.state.vapid = vapid
    client = login(app)
    client.headers.pop("X-CSRF-Token")
    assert client.post("/push/subscribe", json={"endpoint": FCM}, headers=JSON).status_code == 403
    assert client.post("/push/test", json={}, headers=JSON).status_code == 403


def test_reminder_form(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    assert client.post("/settings/reminder", data={"on": "1", "time": "07:30"}).headers["location"] == "/settings?done=reminder"
    page = client.get("/settings").text
    assert 'value="07:30"' in page and 'name="on" value="1" checked' in page
    assert client.post("/settings/reminder", data={"time": "25:00"}).headers["location"] == "/settings?problem=time"
    client.post("/settings/reminder", data={"time": "08:00"})  # box unticked
    assert 'name="on" value="1" checked' not in client.get("/settings").text


def test_signing_a_device_out_drops_its_subscription(tmp_path, vapid):
    from tests.lock_helpers import WINDOWS, device_id

    app, _ = make_app(tmp_path)
    app.state.vapid = vapid
    phone, laptop = login(app), login(app, WINDOWS)
    assert laptop.post("/push/subscribe", json={"endpoint": FCM}, headers=JSON).status_code == 204
    phone.post(f"/settings/devices/{device_id(app, laptop)}/signout")
    c = sqlite3.connect(app.state.db_path)
    assert c.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] == 0
    c.close()


def test_key_script(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=fake-key\n", encoding="utf-8")
    said = []
    assert make_push_key.main([], str(env), said.append) == 0
    first = env.read_text(encoding="utf-8")
    assert "LLM_API_KEY=fake-key" in first and "ANIMUS_VAPID_PRIVATE_KEY=" in first
    value = first.split("ANIMUS_VAPID_PRIVATE_KEY=")[1].strip()
    assert push.load_vapid({"ANIMUS_VAPID_PRIVATE_KEY": value}) is not None
    assert value not in " ".join(said)  # never printed
    assert (env.stat().st_mode & 0o777) == 0o600
    make_push_key.main([], str(env), said.append)
    assert env.read_text(encoding="utf-8") == first and "already exists" in said[-1]
    make_push_key.main(["--replace"], str(env), said.append)
    assert env.read_text(encoding="utf-8") != first and env.read_text(encoding="utf-8").count("ANIMUS_VAPID_PRIVATE_KEY=") == 1


def test_worker_shows_a_fixed_line_and_never_reads_the_push():
    js = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "addEventListener('push'" in js and "A quiet minute to write about today?" in js
    assert "event.data" not in js and "notificationclick" in js
