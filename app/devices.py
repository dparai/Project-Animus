"""Devices: one row per browser that has logged in, with its own app lock.

A device is identified by a random id kept in a long-lived signed cookie (`animus_device`), so its
lock settings survive a log out and a later log in. The session cookie names the device it belongs
to; the gate in app/auth.py refuses a session whose device is missing, signed out or locked.

The lock is enforced by the server: once a device is locked, every page and every request is
refused until `unlock` runs (after a password or passkey check). The page keeps the server's idea
of "in use" fresh with a small ping while it is visible; when the pings stop for longer than the
device's timer (plus a little slack), the server locks the device on its own.

All times are whole seconds since the epoch.
"""
import re
import secrets

from itsdangerous import BadData, URLSafeSerializer

COOKIE_NAME = "animus_device"
COOKIE_MAX_AGE = 400 * 86400  # browsers cap cookie life near this
_SALT = "animus.device.v1"
_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")

LOCK_MODES = ("none", "passkey", "password")
TIMERS = (0, 60, 300, 900)  # seconds in the background before the lock; 0 = at once
PING_S = 30  # how often a visible page pings
SLACK_S = 75  # pings may be late (slow network, throttled timers)
LONG_LIFETIME_S = 30 * 86400  # session life on a device with a lock (the lock guards it)
SUDO_S = 300  # how long a password check allows changes to lock settings
SEEN_EVERY_S = 300  # last_seen_at is rewritten at most this often
MAX_DEVICES = 30

_BROWSERS = (("Edg", "Edge"), ("OPR", "Opera"), ("SamsungBrowser", "Samsung Internet"),
             ("Firefox", "Firefox"), ("FxiOS", "Firefox"), ("CriOS", "Chrome"), ("Chrome", "Chrome"),
             ("Safari", "Safari"))
_SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
            ("Mac OS X", "Mac"), ("CrOS", "Chromebook"), ("Linux", "Linux"))


def label_for(user_agent: str) -> str:
    """'Chrome on Android' from a User-Agent header; never stores the header itself."""
    ua = user_agent or ""
    browser = next((name for mark, name in _BROWSERS if mark in ua), "")
    system = next((name for mark, name in _SYSTEMS if mark in ua), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown device"


def sign_id(secret: str, device_id: str) -> str:
    return URLSafeSerializer(secret, salt=_SALT).dumps(device_id)


def read_id(secret: str | None, cookie: str | None) -> str | None:
    """The device id inside a device cookie, or None. Never raises."""
    if not secret or not cookie or len(cookie) > 512:
        return None
    try:
        value = URLSafeSerializer(secret, salt=_SALT).loads(cookie)
    except BadData:
        return None
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


def get(conn, device_id: str | None):
    if not device_id:
        return None
    return conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()


def attach(conn, device_id: str | None, user_agent: str, now: int, gen: int) -> str:
    """Record a fresh login on this device and return its id (a new one when the cookie is unknown).
    A login is a password or passkey check, so the device starts unlocked."""
    now = int(now)
    label = label_for(user_agent)
    if get(conn, device_id) is not None:
        conn.execute(
            "UPDATE devices SET label = ?, last_login_at = ?, last_seen_at = ?, gen = ?, revoked = 0, "
            "locked = 0, last_active_at = ? WHERE id = ?",
            (label, now, now, gen, now, device_id),
        )
    else:
        device_id = secrets.token_urlsafe(18)
        conn.execute(
            "INSERT INTO devices(id, label, created_at, last_login_at, last_seen_at, gen, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, label, now, now, now, gen, now),
        )
        _prune(conn, gen)
    conn.commit()
    return device_id


def _prune(conn, gen: int) -> None:
    """Keep the table small: beyond MAX_DEVICES, drop the longest-unseen signed-out rows."""
    extra = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] - MAX_DEVICES
    if extra > 0:
        conn.execute(
            "DELETE FROM devices WHERE id IN (SELECT id FROM devices WHERE revoked = 1 OR gen <> ? "
            "ORDER BY last_seen_at LIMIT ?)", (gen, extra))


def signed_in(row, gen: int) -> bool:
    return row is not None and not row["revoked"] and row["gen"] == gen


def lifetime_for(row, base_s: int) -> int:
    """Session life for this device: long only while a lock guards it."""
    return LONG_LIFETIME_S if row is not None and row["lock_mode"] != "none" else base_s


def check_locked(conn, row, now: int) -> bool:
    """True when the device is locked. A timer that ran out is written down, so a later ping
    cannot wake the device again."""
    if row["lock_mode"] == "none":
        return False
    if row["locked"]:
        return True
    if int(now) - row["last_active_at"] > row["lock_after_s"] + SLACK_S:
        conn.execute("UPDATE devices SET locked = 1, sudo_until = 0 WHERE id = ?", (row["id"],))
        conn.commit()
        return True
    return False


def touch_seen(conn, row, now: int) -> None:
    if int(now) - row["last_seen_at"] >= SEEN_EVERY_S:
        conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (int(now), row["id"]))
        conn.commit()


def ping(conn, device_id: str, now: int) -> None:
    """The page is visible and in use. Only ever called for an unlocked device (the gate runs first)."""
    conn.execute("UPDATE devices SET last_active_at = ?, last_seen_at = ? WHERE id = ? AND locked = 0",
                 (int(now), int(now), device_id))
    conn.commit()


def lock(conn, device_id: str) -> None:
    conn.execute("UPDATE devices SET locked = 1, sudo_until = 0 WHERE id = ? AND lock_mode <> 'none'",
                 (device_id,))
    conn.commit()


def unlock(conn, device_id: str, now: int) -> None:
    conn.execute("UPDATE devices SET locked = 0, last_active_at = ?, last_seen_at = ? WHERE id = ?",
                 (int(now), int(now), device_id))
    conn.commit()


def grant_sudo(conn, device_id: str, now: int) -> None:
    conn.execute("UPDATE devices SET sudo_until = ? WHERE id = ?", (int(now) + SUDO_S, device_id))
    conn.commit()


def has_sudo(row, now: int) -> bool:
    return row is not None and row["sudo_until"] > int(now)


def set_lock(conn, device_id: str, mode: str, after_s: int, now: int) -> None:
    if mode not in LOCK_MODES or after_s not in TIMERS:
        raise ValueError("bad lock setting")
    conn.execute(
        "UPDATE devices SET lock_mode = ?, lock_after_s = ?, locked = 0, last_active_at = ? WHERE id = ?",
        (mode, after_s, int(now), device_id))
    conn.commit()


def passkeys_gone(conn) -> None:
    """The last passkey was removed: devices that used it fall back to the password lock."""
    conn.execute("UPDATE devices SET lock_mode = 'password' WHERE lock_mode = 'passkey'")
    conn.commit()


def revoke(conn, device_id: str) -> bool:
    """Sign one device out. Its lock settings stay for the next login."""
    cur = conn.execute("UPDATE devices SET revoked = 1, sudo_until = 0 WHERE id = ?", (device_id,))
    conn.execute("DELETE FROM push_subscriptions WHERE device_id = ?", (device_id,))
    conn.commit()
    return cur.rowcount > 0


def list_all(conn, gen: int) -> list[dict]:
    rows = conn.execute("SELECT * FROM devices ORDER BY last_seen_at DESC").fetchall()
    return [{**dict(r), "signed_in": signed_in(r, gen)} for r in rows]
