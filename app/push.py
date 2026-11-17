"""Daily reminder by Web Push.

What leaves the server is an empty push: no text, no journal data, nothing to encrypt. The service
worker on the device shows a fixed line ("A quiet minute to write?"). The only things a push
service learns are that this server sent a wake-up and when.

The server proves who it is with a VAPID key (ANIMUS_VAPID_PRIVATE_KEY in .env, written by
scripts/make_push_key.py). Subscriptions are accepted only for the known push services of Chrome /
Android, Firefox, Safari / iPhone and Edge, so this code can never be pointed at another host.

The reminder goes out once a day at the chosen local time, and only when nothing has been written
that day. Needs the `cryptography` package; without it or without the key, reminders are off.
"""
import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app import db, tz

log = logging.getLogger("animus.push")

ALLOWED_HOSTS = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")
ENDPOINT_MAX = 1000
MAX_SUBSCRIPTIONS = 20
TOKEN_S = 12 * 3600
TTL_S = 4 * 3600  # a reminder that could not be delivered within 4 hours is dropped
LATE_S = 6 * 3600  # after this long past the chosen time, today's reminder is skipped
MAX_FAILURES = 5
DEFAULT_TIME = "21:00"
_TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_ON, _TIME, _LAST = "reminder_on", "reminder_time", "reminder_last"


@dataclass(frozen=True)
class Vapid:
    private_key: object = field(repr=False)
    public_key: str  # base64url, what the browser subscribes with
    subject: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_vapid(env=os.environ) -> Vapid | None:
    """The signing key, or None when it is missing, malformed or `cryptography` is not installed."""
    raw = (env.get("ANIMUS_VAPID_PRIVATE_KEY") or "").strip().strip("'\"")
    if not raw:
        return None
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        secret = _unb64(raw)
        if len(secret) != 32:
            return None
        key = ec.derive_private_key(int.from_bytes(secret, "big"), ec.SECP256R1())
        point = key.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint)
    except Exception as exc:  # noqa: BLE001 - never log the value
        log.warning("push key unusable (%s)", type(exc).__name__)
        return None
    subject = (env.get("ANIMUS_VAPID_SUBJECT") or "").strip()
    if not subject.startswith(("https://", "mailto:")):
        hosts = [h.strip() for h in (env.get("ANIMUS_ALLOWED_HOSTS") or "").split(",") if "." in h]
        subject = f"https://{hosts[0]}" if hosts else "https://localhost"
    return Vapid(private_key=key, public_key=_b64(point), subject=subject)


def endpoint_ok(url) -> bool:
    """Only https URLs on a known push service, default port, no user info."""
    if not isinstance(url, str) or not 0 < len(url) <= ENDPOINT_MAX:
        return False
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    return any(host == name or host.endswith("." + name) for name in ALLOWED_HOSTS)


def authorization(vapid: Vapid, endpoint: str, now: float) -> str:
    """The VAPID header (RFC 8292): a short-lived ES256 token for this push service only."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    parts = urlsplit(endpoint)
    claims = {"aud": f"https://{parts.netloc}", "exp": int(now) + TOKEN_S, "sub": vapid.subject}
    head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode("ascii")
    r, s = decode_dss_signature(vapid.private_key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={head}.{body}.{signature}, k={vapid.public_key}"


def send(vapid: Vapid, endpoint: str, now: float | None = None, transport=None) -> str:
    """Send one empty push. Returns "ok", "gone" (the subscription no longer exists) or "error"."""
    if not endpoint_ok(endpoint):
        return "gone"
    now = time.time() if now is None else now
    headers = {"Authorization": authorization(vapid, endpoint, now), "TTL": str(TTL_S),
               "Urgency": "normal", "Content-Length": "0"}
    try:
        with httpx.Client(transport=transport, timeout=15.0, follow_redirects=False) as http:
            status = http.post(endpoint, headers=headers).status_code
    except httpx.HTTPError as exc:
        log.info("push failed reason=%s", type(exc).__name__)
        return "error"
    if status in (200, 201, 202, 204):
        return "ok"
    if status in (404, 410):
        return "gone"
    log.info("push failed status=%d", status)
    return "error"


def subscribe(conn, device_id: str | None, endpoint: str, now: int) -> bool:
    if not endpoint_ok(endpoint):
        return False
    conn.execute(
        "INSERT INTO push_subscriptions(device_id, endpoint, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET device_id = excluded.device_id, failures = 0",
        (device_id, endpoint, int(now)))
    # one subscription per device, and a bounded table
    if device_id:
        conn.execute("DELETE FROM push_subscriptions WHERE device_id = ? AND endpoint <> ?", (device_id, endpoint))
    conn.execute(
        "DELETE FROM push_subscriptions WHERE id NOT IN (SELECT id FROM push_subscriptions ORDER BY id DESC LIMIT ?)",
        (MAX_SUBSCRIPTIONS,))
    conn.commit()
    return True


def unsubscribe(conn, device_id: str | None) -> None:
    conn.execute("DELETE FROM push_subscriptions WHERE device_id IS ?", (device_id,))
    conn.commit()


def device_subscribed(conn, device_id: str | None) -> bool:
    if not device_id:
        return False
    return conn.execute("SELECT 1 FROM push_subscriptions WHERE device_id = ?", (device_id,)).fetchone() is not None


def subscription_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0]


def send_all(conn, vapid: Vapid, now: float, transport=None, device_id: str | None = None) -> int:
    """Push to every subscription (or one device's). Drops dead ones. Returns how many went out."""
    query, args = "SELECT id, endpoint FROM push_subscriptions", ()
    if device_id is not None:
        query, args = query + " WHERE device_id = ?", (device_id,)
    sent = 0
    for row in conn.execute(query, args).fetchall():
        result = send(vapid, row["endpoint"], now, transport)
        if result == "ok":
            sent += 1
            conn.execute("UPDATE push_subscriptions SET last_ok_at = ?, failures = 0 WHERE id = ?", (int(now), row["id"]))
        elif result == "gone":
            conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (row["id"],))
        else:
            conn.execute("UPDATE push_subscriptions SET failures = failures + 1 WHERE id = ?", (row["id"],))
            conn.execute("DELETE FROM push_subscriptions WHERE id = ? AND failures >= ?", (row["id"], MAX_FAILURES))
    conn.commit()
    return sent


def _setting(conn, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row and row[0] else ""


def _store(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def get_reminder(conn) -> tuple[bool, str]:
    at = _setting(conn, _TIME)
    return _setting(conn, _ON) == "1", at if _TIME_RE.fullmatch(at) else DEFAULT_TIME


def set_reminder(conn, on: bool, at: str) -> None:
    if not _TIME_RE.fullmatch(at or ""):
        raise ValueError("bad time")
    _store(conn, _ON, "1" if on else "0")
    _store(conn, _TIME, at)
    conn.commit()


def reminder_due(conn, now: datetime) -> str | None:
    """Today's local date when the reminder should go out now, else None."""
    on, at = get_reminder(conn)
    if not on:
        return None
    local = now.astimezone(ZoneInfo(tz.get_timezone(conn)))
    today = local.date().isoformat()
    target = local.replace(hour=int(at[:2]), minute=int(at[3:]), second=0, microsecond=0)
    late = (local - target).total_seconds()
    if late < 0 or late > LATE_S or _setting(conn, _LAST) == today:
        return None
    if conn.execute("SELECT 1 FROM entries WHERE local_date = ? LIMIT 1", (today,)).fetchone():
        return None  # already wrote today: nothing to remind
    return today


def reminder_tick(db_path: str, now: datetime | None = None, vapid: Vapid | None = None, transport=None) -> int:
    """One scheduler pass. Returns how many pushes went out."""
    now = now or datetime.now(timezone.utc)
    conn = db.connect(db_path)
    try:
        today = reminder_due(conn, now)
        if today is None:
            return 0
        vapid = vapid or load_vapid()
        if vapid is None or not subscription_count(conn):
            return 0
        _store(conn, _LAST, today)  # written first: a crash must not turn into a second reminder
        conn.commit()
        sent = send_all(conn, vapid, now.timestamp(), transport)
        log.info("reminder sent count=%d", sent)
        return sent
    finally:
        conn.close()
