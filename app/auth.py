"""Password login, signed session cookies and the gate in front of every page.

One owner, one password. The bcrypt hash and the cookie-signing secret come from `.env`
(ANIMUS_PASSWORD_HASH, ANIMUS_SESSION_SECRET); `scripts/set_password.py` writes both. Without them
the app fails closed: every page answers 503 with a short "login not set up" notice.

The session is stateless: a signed, timestamped cookie (itsdangerous) that carries only a random
CSRF token. It lives 24 hours. Rotating the secret (every password change) logs out every session.

Passwords and hashes are never logged. A failed or blocked login logs a reason code and a keyed
hash of the client's network (the IPv4 address without its last octet, an IPv6 /48).
"""
import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import bcrypt
from itsdangerous import BadData, TimestampSigner, URLSafeTimedSerializer
from starlette.requests import cookie_parser
from starlette.responses import HTMLResponse, Response

COOKIE_NAME = "animus_session"
BCRYPT_COST = 12  # scripts/set_password.py; tests pass a lower cost to hash_password
PASSWORD_MAX = 200  # characters; checked before any hashing
SECRET_MIN = 32  # characters
LIFETIME_S = 86400
COOKIE_MAX = 4096
LOGIN_BODY_MAX = 8 * 1024  # POST /login is a password and a path; anything bigger is refused (413)
FAILED = "That didn't work."
LOGIN_PATH = "/login"
TRUSTED_PROXY = "127.0.0.1"  # Caddy on the same host; only it may set X-Forwarded-*
_SALT = "animus.session.v1"
_GEN_KEY = "session_gen"  # settings-table key
_BCRYPT_RE = re.compile(r"\$2[aby]\$(\d\d)\$[./A-Za-z0-9]{53}")

log = logging.getLogger("animus.auth")


@dataclass(frozen=True)
class AuthSettings:
    password_hash: bytes | None = field(default=None, repr=False)
    secret: str | None = field(default=None, repr=False)
    lifetime_s: int = LIFETIME_S

    @property
    def configured(self) -> bool:
        return bool(self.password_hash) and bool(self.secret)


@dataclass(frozen=True)
class Session:
    csrf: str = field(repr=False)
    issued_at: int = 0


def _unquote(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def load_settings(env=os.environ) -> AuthSettings:
    """Read the hash and secret; anything malformed counts as not configured (fail closed)."""
    raw_hash = _unquote(env.get("ANIMUS_PASSWORD_HASH", ""))
    password_hash = raw_hash.encode("ascii") if _BCRYPT_RE.fullmatch(raw_hash) else None
    secret = _unquote(env.get("ANIMUS_SESSION_SECRET", "")) or None
    if secret is not None and len(secret) < SECRET_MIN:
        secret = None
    return AuthSettings(password_hash=password_hash, secret=secret)


def _prepare(password: str) -> bytes:
    """NFC-normalise, then SHA-256 + base64 so every length fits bcrypt's 72-byte input (44 bytes)."""
    text = unicodedata.normalize("NFC", password)
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()
    return base64.b64encode(digest)


def _acceptable(password) -> bool:
    return isinstance(password, str) and 0 < len(password) <= PASSWORD_MAX


def hash_password(password: str, cost: int = BCRYPT_COST) -> bytes:
    if not _acceptable(password):
        raise ValueError("password must be 1 to 200 characters")
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(rounds=cost))


_dummy_hashes: dict[int, bytes] = {}


def _dummy_hash(cost: int) -> bytes:
    if cost not in _dummy_hashes:
        _dummy_hashes[cost] = bcrypt.hashpw(secrets.token_bytes(16).hex().encode(), bcrypt.gensalt(rounds=cost))
    return _dummy_hashes[cost]


def verify_password(settings: AuthSettings, password: str) -> bool:
    """Constant work: exactly one bcrypt check on every call, whatever the input or configuration."""
    acceptable = _acceptable(password)
    stored = settings.password_hash
    target = stored if stored else _dummy_hash(BCRYPT_COST)
    candidate = _prepare(password if acceptable else "\x00not-a-password")
    try:
        match = bcrypt.checkpw(candidate, target)
    except ValueError:
        match = False
    return bool(match and acceptable and stored)


def _serializer(secret: str, now: float) -> URLSafeTimedSerializer:
    stamp = int(now)

    class _At(TimestampSigner):  # the clock is passed in, so expiry is testable without sleeping
        def get_timestamp(self) -> int:
            return stamp

    return URLSafeTimedSerializer(secret, salt=_SALT, signer=_At,
                                  signer_kwargs={"digest_method": hashlib.sha256})


class SessionGeneration:
    """Server-side logout. Every cookie carries the generation it was issued under; logout bumps
    the generation (persisted in the settings table), so every earlier cookie stops working.
    One in-process object per app: the service runs a single worker."""

    def __init__(self, value: int = 0):
        self.value = value

    @staticmethod
    def _stored(conn) -> int | None:
        """The persisted generation, or None when the row is missing, non-integer or corrupt."""
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (_GEN_KEY,)).fetchone()
        try:
            value = int(row[0]) if row and row[0] is not None else None
        except (TypeError, ValueError):
            return None
        return value if value is not None and value >= 0 else None

    @staticmethod
    def _write(conn, value: int) -> None:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_GEN_KEY, str(value)),
        )
        conn.commit()

    @classmethod
    def load(cls, conn) -> "SessionGeneration":
        """A missing or corrupt row never means generation 0 (that would revive old cookies):
        start from the current time, which is above any generation reached by logouts, and persist it."""
        try:
            stored = cls._stored(conn)
        except Exception as exc:  # noqa: BLE001 - an unreadable row is treated like a corrupt one
            log.warning("session generation unreadable (%s); starting from the clock", type(exc).__name__)
            stored = None
        if stored is not None:
            return cls(stored)
        value = int(time.time())
        log.warning("session generation missing or corrupt; starting from the clock")
        try:
            cls._write(conn, value)
        except Exception as exc:  # noqa: BLE001
            log.error("session generation not saved (%s)", type(exc).__name__)
        return cls(value)

    def bump(self, conn) -> int:
        """Fail closed: the in-memory generation moves first, so the old cookie is dead for this
        process even when the database write fails. Never raises; logs the error class only."""
        try:
            stored = self._stored(conn) if conn is not None else None
        except Exception:  # noqa: BLE001
            stored = None
        new = max(self.value, stored or 0) + 1
        self.value = new
        try:
            if conn is None:
                raise OSError("no connection")
            self._write(conn, new)
        except Exception as exc:  # noqa: BLE001
            log.error("session generation not saved (%s)", type(exc).__name__)
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        return new


def issue_session(settings: AuthSettings, now: float, gen: int = 0) -> str:
    if not settings.secret:
        raise ValueError("no session secret configured")
    return _serializer(settings.secret, now).dumps({"c": secrets.token_urlsafe(32), "g": gen})


def read_session(settings: AuthSettings, cookie: str, now: float, gen: int = 0) -> Session | None:
    """A valid, unexpired session of the current generation, or None. Never raises on a bad cookie."""
    if not settings.secret or not cookie or len(cookie) > COOKIE_MAX:
        return None
    try:
        data, stamp = _serializer(settings.secret, now).loads(
            cookie, max_age=settings.lifetime_s, return_timestamp=True
        )
    except BadData:
        return None
    except Exception:  # noqa: BLE001 - a malformed cookie must never become a 500
        return None
    if not isinstance(data, dict):
        return None
    csrf, cookie_gen = data.get("c"), data.get("g")
    if not isinstance(csrf, str) or len(csrf) < 32:
        return None
    if type(cookie_gen) is not int or cookie_gen != gen:  # logged out since this cookie was issued
        return None
    return Session(csrf=csrf, issued_at=int(stamp.timestamp()))


def safe_next(value: str | None) -> str:
    """Only a same-site relative path survives; anything else becomes "/"."""
    if not value or len(value) > 2048:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in value):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc or parts.path in (LOGIN_PATH, "/logout"):
        return "/"
    return value


def _header_values(scope, name: bytes) -> list[str]:
    return [v.decode("latin-1") for k, v in scope.get("headers", []) if k == name]


def _peer(scope) -> str:
    client = scope.get("client")
    return (client[0] if client else "") or ""


def client_ip(scope) -> str:
    """The direct peer, or, when the peer is the local proxy, the right-most X-Forwarded-For entry
    (the one Caddy appended). Earlier entries are client-controlled and ignored."""
    # Note: uvicorn's proxy-headers handling (on by default, trusting 127.0.0.1) normally rewrites
    # scope["client"] to the real client before the app sees it, so this branch is a second layer:
    # it only acts when the peer still reads 127.0.0.1 (proxy headers off, or no usable header).
    peer = _peer(scope)
    if peer == TRUSTED_PROXY:
        values = _header_values(scope, b"x-forwarded-for")
        if values:
            last = ",".join(values).split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(last))
            except ValueError:
                pass
    return peer


def is_https(scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    if _peer(scope) != TRUSTED_PROXY:
        return False
    values = _header_values(scope, b"x-forwarded-proto")
    return bool(values) and values[-1].split(",")[-1].strip().lower() == "https"


def ip_tag(ip: str, key: str | None) -> str:
    """Keyed hash of the client's network, for logs: no address, no last octet."""
    try:
        addr = ipaddress.ip_address(ip)
        prefix = 24 if addr.version == 4 else 48
        network = str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))
    except ValueError:
        network = "unknown"
    mac = hmac.new((key or "animus").encode(), network.encode(), hashlib.sha256)
    return mac.hexdigest()[:12]


def set_session_cookie(response: Response, value: str, *, secure: bool, max_age: int) -> None:
    response.set_cookie(COOKIE_NAME, value, max_age=max_age, path="/", secure=secure,
                        httponly=True, samesite="lax")


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=secure, httponly=True, samesite="lax")


NOT_SET_UP = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
    "<meta name=\"color-scheme\" content=\"dark\"><title>Animus</title></head>"
    "<body><p>Login is not set up yet. Run scripts/set_password.py on the server, "
    "then restart the app.</p></body></html>"
)
NO_STORE = {"Cache-Control": "no-store"}


class AuthMiddleware:
    """Pure ASGI gate. Open: /login (GET/POST), /healthz, /static/*. Everything else needs a valid
    session, which is stored in scope["state"]["session"]."""

    def __init__(self, app, settings: AuthSettings, clock=time.time,
                 generation: SessionGeneration | None = None):
        self.app = app
        self.settings = settings
        self.clock = clock
        self.generation = generation if generation is not None else SessionGeneration()

    def _session(self, scope) -> Session | None:
        for raw in _header_values(scope, b"cookie"):
            value = cookie_parser(raw).get(COOKIE_NAME)
            if value:
                return read_session(self.settings, value, self.clock(), self.generation.value)
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":  # the app has none; refuse rather than leave a gap
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        # Allow-list: /healthz, and EVERY route under /static/ is public (no session needed).
        # Never mount anything carrying journal data under /static/.
        if path == "/healthz" or path.startswith("/static/"):
            await self.app(scope, receive, send)
            return
        if not self.settings.configured:
            await HTMLResponse(NOT_SET_UP, status_code=503, headers=NO_STORE)(scope, receive, send)
            return
        session = self._session(scope)
        if session is not None:
            scope.setdefault("state", {})["session"] = session
        if path == LOGIN_PATH and scope["method"] == "POST":
            receive = await _bounded_body(scope, receive, send, LOGIN_BODY_MAX)
            if receive is None:
                return
        if session is not None or path == LOGIN_PATH:
            await self.app(scope, receive, send)
            return
        await self._refuse(scope)(scope, receive, send)

    @staticmethod
    def _refuse(scope) -> Response:
        headers = dict(scope.get("headers", []))
        if b"hx-request" in headers:  # HTMX: navigate the whole page, never swap the form in
            return Response(status_code=401, headers={"HX-Redirect": LOGIN_PATH, **NO_STORE})
        location = LOGIN_PATH
        if scope["method"] in ("GET", "HEAD"):
            target = scope["path"]
            query = scope.get("query_string", b"").decode("latin-1")
            if query:
                target += "?" + query
            target = safe_next(target)
            if target != "/":
                location += "?next=" + quote(target, safe="")
        return Response(status_code=303, headers={"Location": location, **NO_STORE})


async def _bounded_body(scope, receive, send, limit: int):
    """Read the whole request body (at most `limit` bytes) before any form parsing.

    Returns a receive callable that replays it, or None after answering 413 (too large) itself.
    """
    too_large = Response("Request too large.", status_code=413, media_type="text/plain")
    for raw in _header_values(scope, b"content-length"):
        try:
            declared = int(raw)
        except ValueError:
            declared = limit + 1
        if declared > limit:
            await too_large(scope, receive, send)
            return None
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":  # the client went away; let the app see that
            first = message

            async def gone():
                return first

            return gone
        body = message.get("body", b"")
        size += len(body)
        if size > limit:
            await too_large(scope, receive, send)
            return None
        chunks.append(body)
        if not message.get("more_body", False):
            break
    data = b"".join(chunks)
    replayed = False

    async def replay():
        nonlocal replayed
        if not replayed:
            replayed = True
            return {"type": "http.request", "body": data, "more_body": False}
        return await receive()

    return replay
