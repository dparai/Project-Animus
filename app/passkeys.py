"""Passkeys (WebAuthn): fingerprint, face or screen lock in place of typing the password.

A passkey can unlock a locked device and can log in. Every check requires user verification (the
phone asks for the fingerprint, face or PIN), and the browser binds each signature to this site's
name, so a look-alike site cannot reuse one.

The `webauthn` package does the cryptography. When it is not installed, AVAILABLE is False and the
app simply offers the password everywhere.

Challenges live in memory for five minutes and work once (one process, one worker).
"""
import ipaddress
import json
import secrets

try:
    import webauthn
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (
        AuthenticatorSelectionCriteria,
        PublicKeyCredentialDescriptor,
        ResidentKeyRequirement,
        UserVerificationRequirement,
    )
    AVAILABLE = True
except ImportError:  # the package is optional
    AVAILABLE = False

USER_ID = b"animus-owner"
CHALLENGE_S = 300
MAX_CHALLENGES = 200
MAX_PASSKEYS = 10
LABEL_MAX = 60


class Challenges:
    """handle -> (challenge, purpose, bound device, expiry). Single use, bounded."""

    def __init__(self):
        self._items: dict[str, tuple[bytes, str, str | None, int]] = {}

    def put(self, challenge: bytes, purpose: str, device: str | None, now: int) -> str:
        now = int(now)
        for key in [k for k, v in self._items.items() if v[3] <= now]:
            del self._items[key]
        while len(self._items) >= MAX_CHALLENGES:
            del self._items[next(iter(self._items))]
        handle = secrets.token_urlsafe(18)
        self._items[handle] = (challenge, purpose, device, now + CHALLENGE_S)
        return handle

    def take(self, handle, purpose: str, device: str | None, now: int) -> bytes | None:
        item = self._items.pop(handle, None) if isinstance(handle, str) else None
        if item is None:
            return None
        challenge, kind, bound, expires = item
        if kind != purpose or bound != device or expires <= int(now):
            return None
        return challenge


def site(host_header: str, https: bool) -> tuple[str, str] | None:
    """(rp_id, origin) for this request, or None where passkeys cannot work (an IP address).
    The Host header has already passed the allowed-hosts check."""
    host = (host_header or "").strip().lower()
    name = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
    if not name or name.startswith("["):
        return None
    try:
        ipaddress.ip_address(name)
        return None
    except ValueError:
        pass
    return name, f"{'https' if https else 'http'}://{host}"


def count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM passkeys").fetchone()[0]


def list_all(conn) -> list[dict]:
    rows = conn.execute("SELECT id, label, device_id, created_at, last_used_at FROM passkeys ORDER BY id")
    return [dict(r) for r in rows]


def remove(conn, passkey_id: int) -> bool:
    cur = conn.execute("DELETE FROM passkeys WHERE id = ?", (passkey_id,))
    conn.commit()
    return cur.rowcount > 0


def _descriptors(conn):
    return [PublicKeyCredentialDescriptor(id=bytes(r[0]))
            for r in conn.execute("SELECT credential_id FROM passkeys ORDER BY id")]


def registration_options(conn, rp_id: str, challenges: Challenges, device: str, now: int) -> dict:
    options = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name="Animus", user_name="owner", user_id=USER_ID, user_display_name="Animus journal",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=_descriptors(conn),
    )
    handle = challenges.put(options.challenge, "register", device, now)
    return {"handle": handle, "options": json.loads(webauthn.options_to_json(options))}


def register(conn, rp_id: str, origin: str, challenges: Challenges, handle, credential, label: str,
             device: str, now: int) -> bool:
    challenge = challenges.take(handle, "register", device, now)
    if challenge is None or not isinstance(credential, dict) or count(conn) >= MAX_PASSKEYS:
        return False
    try:
        verified = webauthn.verify_registration_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, require_user_verification=True)
    except Exception:  # noqa: BLE001 - any malformed or failed response is simply "no"
        return False
    try:
        conn.execute(
            "INSERT INTO passkeys(credential_id, public_key, sign_count, label, device_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (verified.credential_id, verified.credential_public_key, verified.sign_count,
             " ".join((label or "").split())[:LABEL_MAX], device, int(now)))
        conn.commit()
    except Exception:  # noqa: BLE001 - the same credential twice
        conn.rollback()
        return False
    return True


def authentication_options(conn, rp_id: str, challenges: Challenges, purpose: str, device: str | None,
                           now: int) -> dict:
    options = webauthn.generate_authentication_options(
        rp_id=rp_id, allow_credentials=_descriptors(conn),
        user_verification=UserVerificationRequirement.REQUIRED)
    handle = challenges.put(options.challenge, purpose, device, now)
    return {"handle": handle, "options": json.loads(webauthn.options_to_json(options))}


def authenticate(conn, rp_id: str, origin: str, challenges: Challenges, handle, credential, purpose: str,
                 device: str | None, now: int) -> bool:
    """True only for a fresh, verified signature from a stored passkey."""
    challenge = challenges.take(handle, purpose, device, now)
    if challenge is None or not isinstance(credential, dict):
        return False
    try:
        raw_id = base64url_to_bytes(str(credential.get("rawId") or credential.get("id") or ""))
    except Exception:  # noqa: BLE001
        return False
    row = conn.execute("SELECT id, public_key, sign_count FROM passkeys WHERE credential_id = ?",
                       (raw_id,)).fetchone()
    if row is None:
        return False
    try:
        verified = webauthn.verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"], require_user_verification=True)
    except Exception:  # noqa: BLE001
        return False
    conn.execute("UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE id = ?",
                 (verified.new_sign_count, int(now), row["id"]))
    conn.commit()
    return True
