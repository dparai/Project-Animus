"""Passkeys: adding one, unlocking and logging in with it, and everything that must be refused.

A small software authenticator stands in for the phone: it holds a real P-256 key and signs the
same structures a phone would, so the server's checks run for real.
"""
import hashlib
import json
import os
import sqlite3

import pytest

pytest.importorskip("webauthn")
cbor2 = pytest.importorskip("cbor2")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from webauthn.helpers import bytes_to_base64url  # noqa: E402

from app import passkeys  # noqa: E402
from tests.auth_helpers import TEST_PASSWORD, anon_client  # noqa: E402
from tests.lock_helpers import device_id, login, make_app, refresh_csrf, set_lock  # noqa: E402

ORIGIN = "http://testserver"
JSON = {"X-Requested-With": "fetch"}


class Phone:
    """A fake authenticator with one credential."""

    def __init__(self, rp_id="testserver", origin=ORIGIN, verify_user=True):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.rp_id, self.origin, self.verify_user = rp_id, origin, verify_user
        self.count = 0

    def _auth_data(self, attested: bool) -> bytes:
        self.count += 1
        flags = 0x01 | (0x04 if self.verify_user else 0) | (0x40 if attested else 0)
        data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + self.count.to_bytes(4, "big")
        if attested:
            numbers = self.key.public_key().public_numbers()
            cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
            data += bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose
        return data

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin, "crossOrigin": False}).encode()

    def _base(self):
        cid = bytes_to_base64url(self.credential_id)
        return {"id": cid, "rawId": cid, "type": "public-key", "clientExtensionResults": {}}

    def create(self, options: dict) -> dict:
        client_data = self._client_data("webauthn.create", options["challenge"])
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": self._auth_data(True)})
        return {**self._base(), "response": {"clientDataJSON": bytes_to_base64url(client_data),
                                             "attestationObject": bytes_to_base64url(attestation)}}

    def get(self, options: dict) -> dict:
        client_data = self._client_data("webauthn.get", options["challenge"])
        auth_data = self._auth_data(False)
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {**self._base(), "response": {"clientDataJSON": bytes_to_base64url(client_data),
                                             "authenticatorData": bytes_to_base64url(auth_data),
                                             "signature": bytes_to_base64url(signature)}}


def add_passkey(client, phone) -> None:
    opts = client.post("/settings/passkeys/options", json={}, headers=JSON)
    assert opts.status_code == 200, opts.text
    body = opts.json()
    assert body["options"]["rp"]["id"] == "testserver"
    assert body["options"]["authenticatorSelection"]["userVerification"] == "required"
    r = client.post("/settings/passkeys/verify", json={"handle": body["handle"], "credential": phone.create(body["options"])}, headers=JSON)
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text


def check(client, base: str, phone, extra=None, tamper=None):
    opts = client.post(base + "/options", json={}, headers=JSON)
    assert opts.status_code == 200, opts.text
    body = opts.json()
    assert body["options"]["userVerification"] == "required"
    payload = {"handle": body["handle"], "credential": phone.get(body["options"]), **(extra or {})}
    if tamper:
        tamper(payload)
    return client.post(base + "/verify", json=payload, headers=JSON), payload


@pytest.fixture
def ready(tmp_path):
    """An app, a logged-in client and a phone whose passkey is registered."""
    app, clock = make_app(tmp_path)
    client = login(app)
    phone = Phone()
    add_passkey(client, phone)
    return app, clock, client, phone


def count(app) -> int:
    conn = sqlite3.connect(app.state.db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0]
    finally:
        conn.close()


def test_add_passkey_then_choose_fingerprint_lock(ready):
    app, _, client, _ = ready
    assert count(app) == 1
    page = client.get("/settings").text
    assert "Chrome on Android" in page and "Remove</button>" in page
    set_lock(client, "passkey", 60)
    home = client.get("/").text
    assert 'data-mode="passkey"' in home and 'id="lock-passkey"' in home and "/static/passkey.js" in home


def test_adding_a_passkey_needs_the_password_again(tmp_path):
    app, clock = make_app(tmp_path)
    client = login(app)
    clock.tick(301)
    assert client.post("/settings/passkeys/options", json={}, headers=JSON).status_code == 403
    client.post("/settings/confirm", data={"password": TEST_PASSWORD})
    opts = client.post("/settings/passkeys/options", json={}, headers=JSON).json()
    clock.tick(301)  # the five minutes ran out between the two steps
    r = client.post("/settings/passkeys/verify", json={"handle": opts["handle"], "credential": Phone().create(opts["options"])}, headers=JSON)
    assert r.status_code == 403 and count(app) == 0


def test_registration_refuses_wrong_site_no_user_check_and_replays(tmp_path):
    app, _ = make_app(tmp_path)
    client = login(app)
    for phone in (Phone(origin="https://evil.example"), Phone(rp_id="evil.example"), Phone(verify_user=False)):
        opts = client.post("/settings/passkeys/options", json={}, headers=JSON).json()
        r = client.post("/settings/passkeys/verify", json={"handle": opts["handle"], "credential": phone.create(opts["options"])}, headers=JSON)
        assert r.status_code == 422
    good = Phone()
    opts = client.post("/settings/passkeys/options", json={}, headers=JSON).json()
    credential = good.create(opts["options"])
    assert client.post("/settings/passkeys/verify", json={"handle": opts["handle"], "credential": credential}, headers=JSON).status_code == 200
    again = client.post("/settings/passkeys/verify", json={"handle": opts["handle"], "credential": credential}, headers=JSON)
    assert again.status_code == 422 and count(app) == 1
    for junk in ({}, {"handle": "x"}, {"handle": opts["handle"], "credential": "text"}, {"handle": 5, "credential": {}}):
        assert client.post("/settings/passkeys/verify", json=junk, headers=JSON).status_code == 422


def test_unlock_with_passkey(ready):
    app, _, client, phone = ready
    set_lock(client, "passkey", 60)
    refresh_csrf(app, client)
    client.post("/lock")
    assert client.get("/").status_code == 303
    r, _ = check(client, "/unlock/passkey", phone)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "Max-Age=2592000" in r.headers["set-cookie"]
    assert client.get("/").status_code == 200
    # the password still opens a fingerprint lock
    client.post("/lock")
    assert client.post("/unlock", data={"password": TEST_PASSWORD}).status_code == 303


def test_unlock_refuses_bad_signatures(ready):
    app, _, client, phone = ready
    set_lock(client, "passkey", 60)
    refresh_csrf(app, client)
    client.post("/lock")

    def other_site(payload):
        payload["credential"] = Phone(origin="https://evil.example").get({"challenge": "x"})

    stranger = Phone()  # a real key the server has never seen
    cases = [
        (phone, lambda p: p["credential"]["response"].update(signature=bytes_to_base64url(os.urandom(70)))),
        (phone, lambda p: p.update(handle="not-a-handle")),
        (stranger, None),
        (phone, other_site),
    ]
    for who, tamper in cases:
        r, _ = check(client, "/unlock/passkey", who, tamper=tamper)
        assert r.status_code == 401, r.text
    phone.verify_user = False
    assert check(client, "/unlock/passkey", phone)[0].status_code == 401
    phone.origin, phone.verify_user = "https://evil.example", True
    assert check(client, "/unlock/passkey", phone)[0].status_code == 429  # five failures: blocked
    assert client.get("/").status_code == 303  # never unlocked


def test_a_signature_works_once_and_only_for_its_purpose(ready):
    app, _, client, phone = ready
    set_lock(client, "passkey", 60)
    refresh_csrf(app, client)
    client.post("/lock")
    r, payload = check(client, "/unlock/passkey", phone)
    assert r.status_code == 200
    client.post("/lock")
    assert client.post("/unlock/passkey/verify", json=payload, headers=JSON).status_code == 401  # replay
    # a challenge issued for logging in cannot unlock, and the other way round
    anon = anon_client(app)
    login_opts = anon.post("/login/passkey/options", json={}, headers=JSON).json()
    crossed = {"handle": login_opts["handle"], "credential": phone.get(login_opts["options"])}
    assert client.post("/unlock/passkey/verify", json=crossed, headers=JSON).status_code == 401
    unlock_opts = client.post("/unlock/passkey/options", json={}, headers=JSON).json()
    crossed = {"handle": unlock_opts["handle"], "credential": phone.get(unlock_opts["options"])}
    assert anon.post("/login/passkey/verify", json=crossed, headers=JSON).status_code == 401
    # a challenge handed to one device is no use on another
    second = login(app)
    add_lock = set_lock(second, "password", 60)
    assert add_lock.status_code == 303
    refresh_csrf(app, second)
    second.post("/lock")
    opts = client.post("/unlock/passkey/options", json={}, headers=JSON).json()
    crossed = {"handle": opts["handle"], "credential": phone.get(opts["options"])}
    assert second.post("/unlock/passkey/verify", json=crossed, headers=JSON).status_code == 401
    assert second.get("/").status_code == 303


def test_expired_challenge_is_refused(ready):
    app, clock, client, phone = ready
    set_lock(client, "passkey", 900)
    refresh_csrf(app, client)
    client.post("/lock")
    opts = client.post("/unlock/passkey/options", json={}, headers=JSON).json()
    clock.tick(passkeys.CHALLENGE_S + 1)
    r = client.post("/unlock/passkey/verify", json={"handle": opts["handle"], "credential": phone.get(opts["options"])}, headers=JSON)
    assert r.status_code == 401


def test_login_with_passkey(ready):
    app, _, client, phone = ready
    anon = anon_client(app)
    assert 'id="login-passkey"' in anon.get("/login").text
    r, _ = check(anon, "/login/passkey", phone, extra={"next": "/entries"})
    assert r.status_code == 200 and r.json() == {"ok": True, "next": "/entries"}
    assert anon.get("/").status_code == 200
    assert device_id(app, anon) != device_id(app, client)
    # an outside address is never a redirect target
    other = anon_client(app)
    r, _ = check(other, "/login/passkey", phone, extra={"next": "https://evil.example/"})
    assert r.json()["next"] == "/"


def test_passkey_login_is_refused_and_rate_limited(ready):
    app, _, _, _ = ready
    anon = anon_client(app)
    stranger = Phone()
    for _ in range(5):
        r, _ = check(anon, "/login/passkey", stranger)
        assert r.status_code == 401 and "set-cookie" not in r.headers
    assert anon.post("/login/passkey/options", json={}, headers=JSON).status_code == 429
    assert anon.post("/login/passkey/verify", json={}, headers=JSON).status_code == 429
    assert anon.get("/").status_code == 303
    assert anon.post("/login/passkey/verify", content=b"x" * 20000, headers={"Content-Type": "application/json"}).status_code == 413


def test_no_passkeys_means_no_passkey_login(tmp_path):
    app, _ = make_app(tmp_path)
    anon = anon_client(app)
    assert 'id="login-passkey"' not in anon.get("/login").text
    assert anon.post("/login/passkey/options", json={}, headers=JSON).status_code == 404
    assert anon.get("/login/passkey/options").status_code == 303  # only POST is open without a session


def test_removing_the_last_passkey_falls_back_to_the_password_lock(ready):
    app, clock, client, _ = ready
    set_lock(client, "passkey", 60)
    refresh_csrf(app, client)
    for _ in range(11):  # the page stays open until the password check from the login has run out
        clock.tick(30)
        assert client.post("/lock/ping").status_code == 204
    assert client.post("/settings/passkeys/1/remove").headers["location"].endswith("confirm")
    assert count(app) == 1
    client.post("/settings/confirm", data={"password": TEST_PASSWORD})
    assert client.post("/settings/passkeys/1/remove").headers["location"] == "/settings?done=passkey"
    assert count(app) == 0
    home = client.get("/").text
    assert 'data-mode="password"' in home and 'id="lock-passkey"' not in home


def test_site_name_rules():
    assert passkeys.site("journal.example.com", True) == ("journal.example.com", "https://journal.example.com")
    assert passkeys.site("localhost:8199", False) == ("localhost", "http://localhost:8199")
    assert passkeys.site("127.0.0.1:8100", False) is None and passkeys.site("[::1]:8100", False) is None
    assert passkeys.site("", True) is None


def test_challenge_store_is_bounded_and_single_use():
    store = passkeys.Challenges()
    handles = [store.put(b"c%d" % i, "login", None, 1000) for i in range(passkeys.MAX_CHALLENGES + 20)]
    assert len(store._items) == passkeys.MAX_CHALLENGES
    assert store.take(handles[0], "login", None, 1001) is None  # pushed out
    last = handles[-1]
    assert store.take(last, "unlock", None, 1001) is None  # wrong purpose burns it
    assert store.take(last, "login", None, 1001) is None
    fresh = store.put(b"z", "unlock", "dev", 1000)
    assert store.take(fresh, "unlock", "dev", 1000 + passkeys.CHALLENGE_S) is None
    assert store.take(["x"], "unlock", "dev", 1000) is None
