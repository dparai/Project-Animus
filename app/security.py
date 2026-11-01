"""Cross-site request guard for the unsafe routes.

The app is reached through an SSH tunnel on localhost, so any web page open in the owner's browser
could submit a form to it. Browsers attach Origin (and Sec-Fetch-Site) to such requests; this
middleware refuses them. Requests with neither header (curl, tests) pass.

Phase 4 added login and CSRF tokens (app/csrf.py); this guard stays in front as a second layer.

`Referrer-Policy: no-referrer` (app/headers.py) makes browsers send `Origin: null` on a plain
same-origin form POST. That one case is let through only when the browser also says
`Sec-Fetch-Site: same-origin` (a header pages cannot set). A null Origin without it is refused.
"""
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
REFUSED = "Cross-site request refused."


def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    for pattern in allowed:
        if pattern == "*" or host == pattern:
            return True
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
    return False


class CrossSiteGuardMiddleware:
    def __init__(self, app, allowed_hosts):
        self.app = app
        self.allowed = tuple(h.lower() for h in allowed_hosts)

    def _refuse(self, headers: dict[bytes, bytes]) -> bool:
        fetch_site = headers.get(b"sec-fetch-site", b"").decode("latin-1").strip().lower()
        if fetch_site == "cross-site":
            return True
        raw = headers.get(b"origin")
        if raw is None:
            return False
        if raw.strip().lower() == b"null" and fetch_site == "same-origin":
            return False  # a same-origin form post under Referrer-Policy: no-referrer
        try:
            host = urlsplit(raw.decode("latin-1").strip()).hostname  # drops the port
        except ValueError:
            return True
        return host is None or not _host_allowed(host.lower(), self.allowed)  # "null" has no host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            if self._refuse(dict(scope["headers"])):
                await PlainTextResponse(REFUSED, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
