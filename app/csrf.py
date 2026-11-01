"""CSRF tokens for every unsafe request (pure ASGI, innermost: it runs after AuthMiddleware).

The token is the random `csrf` value inside the signed session cookie (app/auth.py). Templates get
it as {{ csrf_token }}: `<body hx-headers=...>` makes HTMX send it as `X-CSRF-Token`, and plain
`<form method="post">` forms carry it in a hidden `csrf_token` field.

A request whose method is not GET/HEAD/OPTIONS passes only with a matching token, compared with
hmac.compare_digest. The header wins when present. Without it, a urlencoded form body is read
(bounded), its `csrf_token` field is checked, and the body is replayed downstream unchanged.
Anything else gets 403 "Request refused." before the route runs, so nothing is written.

POST /login is exempt: no session (and so no token) exists yet. It still passes the Origin /
Sec-Fetch-Site guard (app/security.py), and the session cookie is SameSite=Lax.
The CrossSiteGuard stays in front of this as a second layer.
"""
import hmac
from urllib.parse import parse_qs

from starlette.responses import PlainTextResponse

from app.headers import BODY_MAX, read_body, replaying

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
EXEMPT = frozenset({("POST", "/login")})
HEADER = b"x-csrf-token"
FIELD = "csrf_token"
FORM_TYPE = "application/x-www-form-urlencoded"
REFUSED = "Request refused."


def same_token(given: str | bytes | None, expected: str | None) -> bool:
    if not given or not expected:
        return False
    if isinstance(given, str):
        given = given.encode("utf-8", "surrogatepass")
    return hmac.compare_digest(given, expected.encode("ascii"))


def _content_type(headers) -> str:
    raw = headers.get(b"content-type", b"").decode("latin-1")
    return raw.split(";", 1)[0].strip().lower()


def form_token(body: bytes) -> str | None:
    try:
        fields = parse_qs(body.decode("latin-1"), keep_blank_values=True, max_num_fields=1000)
    except ValueError:  # too many fields
        return None
    values = fields.get(FIELD)
    return values[0] if values else None


class CsrfMiddleware:
    def __init__(self, app, max_bytes: int = BODY_MAX):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope["method"] in SAFE_METHODS
                or (scope["method"], scope["path"]) in EXEMPT):
            await self.app(scope, receive, send)
            return
        session = scope.get("state", {}).get("session")
        expected = session.csrf if session is not None else None
        headers = dict(scope.get("headers", []))
        given = headers.get(HEADER)
        if given is not None:
            ok = same_token(given, expected)
        elif expected is not None and _content_type(headers) == FORM_TYPE:
            body, gone = await read_body(receive, self.max_bytes)
            ok = body is not None and same_token(form_token(body), expected)
            if ok:
                receive = replaying(body, receive)
        else:
            ok = False
        if not ok:
            await PlainTextResponse(REFUSED, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)
