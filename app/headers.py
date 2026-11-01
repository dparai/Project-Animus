"""Security headers, the per-request CSP nonce and the request body limit (pure ASGI).

SecurityHeadersMiddleware adds nosniff, no-referrer, frame DENY and a Content-Security-Policy to
every response it sees, and `Cache-Control: no-store` to every non-static response that has not
set its own. Each request gets a fresh nonce in scope["state"]["csp_nonce"]; templates put it on
their inline <script> tags as {{ csp_nonce }}.

BodyLimitMiddleware refuses a request body over 400 KB with 413, both by Content-Length and by
counting the streamed bytes. It buffers the (bounded) body and replays it downstream.
"""
import secrets

from starlette.datastructures import MutableHeaders
from starlette.responses import Response

BODY_MAX = 400_000  # bytes; an entry of 20,000 emoji percent-encodes to about 240 KB, which must still reach the 422
TOO_LARGE = "Request too large."
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

BASE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def content_security_policy(nonce: str) -> str:
    return "; ".join([
        "default-src 'self'",
        # Scripts: same origin only (HTMX and Tailwind are served from /static/). Inline scripts run only with this request's nonce; no 'unsafe-inline', no 'unsafe-eval'.
        f"script-src 'self' 'nonce-{nonce}'",
        # Styles keep 'unsafe-inline' (no nonce here, or browsers would ignore it): the templates use
        # <style> blocks and style="" attributes, and the Tailwind Play CDN and htmx's indicator
        # styles inject <style> elements at run time. Styles cannot run code.
        "style-src 'self' 'unsafe-inline'",
        "font-src 'self'",
        "img-src 'self' data:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])


# For responses written outside the app (413 here): nothing on them may load or run anything.
BARE_HEADERS = {
    **BASE_HEADERS,
    "Content-Security-Policy": "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(18)
        scope.setdefault("state", {})["csp_nonce"] = nonce
        static = scope["path"].startswith("/static/")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in BASE_HEADERS.items():
                    headers[name] = value
                headers["Content-Security-Policy"] = content_security_policy(nonce)
                if not static and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"  # journal pages never sit in a cache
            await send(message)

        await self.app(scope, receive, send_with_headers)


def too_large() -> Response:
    return Response(TOO_LARGE, status_code=413, media_type="text/plain", headers=BARE_HEADERS)


def _header_values(scope, name: bytes) -> list[bytes]:
    return [v for k, v in scope.get("headers", []) if k == name]


async def read_body(receive, limit: int):
    """Read the whole body, at most `limit` bytes.

    Returns (body, None) on success, (None, message) when the client went away first (the message
    is the disconnect, to be replayed), or (None, None) when the body is over the limit.
    """
    chunks, size = [], 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None, message
        body = message.get("body", b"")
        size += len(body)
        if size > limit:
            return None, None
        chunks.append(body)
        if not message.get("more_body", False):
            return b"".join(chunks), None


def replaying(body: bytes, receive):
    """A receive callable that hands the buffered body over once, then defers to the original."""
    sent = False

    async def replay():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return replay


def _once(message):
    async def gone():
        return message

    return gone


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int = BODY_MAX):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        lengths = _header_values(scope, b"content-length")
        chunked = bool(_header_values(scope, b"transfer-encoding"))
        if not lengths and not chunked and scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)  # no body at all
            return
        for raw in lengths:
            try:
                declared = int(raw)
            except ValueError:
                declared = -1
            if declared < 0 or declared > self.max_bytes:
                await too_large()(scope, receive, send)
                return
        body, gone = await read_body(receive, self.max_bytes)
        if body is None and gone is None:
            await too_large()(scope, receive, send)
            return
        await self.app(scope, _once(gone) if body is None else replaying(body, receive), send)
