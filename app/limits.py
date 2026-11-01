"""In-memory login rate limit (spec 5.7): 5 failed logins from one client IP block it for 15 minutes.

Single process, single worker, so a dict is enough. A block is fixed once set: further attempts,
the right password included, do not shorten or extend it. Memory stays bounded.

Attempts are reserved before the (slow) password check: recorded failures plus attempts still in
flight never exceed the limit, so a burst of parallel guesses cannot get more than 5 checks.
"""
import math
import time

MAX_TRACKED = 10_000


class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_s: int = 900, clock=time.monotonic):
        self.max_failures = max_failures
        self.window_s = window_s
        self.clock = clock
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._in_flight: dict[str, int] = {}

    def blocked(self, ip: str) -> int:
        """Seconds left on the block, rounded up; 0 when not blocked."""
        until = self._blocked_until.get(ip)
        if until is None:
            return 0
        left = until - self.clock()
        if left <= 0:
            del self._blocked_until[ip]
            return 0
        return max(1, math.ceil(left))

    def _recent(self, ip: str, now: float) -> list[float]:
        return [t for t in self._failures.get(ip, ()) if now - t < self.window_s]

    def reserve(self, ip: str) -> bool:
        """Claim one password check for this IP; False when blocked or the budget is used up."""
        if self.blocked(ip):
            return False
        if len(self._recent(ip, self.clock())) + self._in_flight.get(ip, 0) >= self.max_failures:
            return False
        self._in_flight[ip] = self._in_flight.get(ip, 0) + 1
        return True

    def release(self, ip: str) -> None:
        left = self._in_flight.get(ip, 0) - 1
        if left > 0:
            self._in_flight[ip] = left
        else:
            self._in_flight.pop(ip, None)

    def fail(self, ip: str) -> None:
        if self.blocked(ip):
            return
        now = self.clock()
        recent = self._recent(ip, now)
        recent.append(now)
        if len(recent) >= self.max_failures:
            self._failures.pop(ip, None)
            self._blocked_until[ip] = now + self.window_s
        else:
            self._failures[ip] = recent
        self._prune(now)

    def clear(self, ip: str) -> None:
        """Forget recorded failures (after a successful login). A block is never lifted early."""
        self._failures.pop(ip, None)

    def _prune(self, now: float) -> None:
        if len(self._failures) + len(self._blocked_until) <= MAX_TRACKED:
            return
        for ip in [ip for ip, until in self._blocked_until.items() if until <= now]:
            del self._blocked_until[ip]
        for ip in [ip for ip, ts in self._failures.items() if now - ts[-1] >= self.window_s]:
            del self._failures[ip]
        while self._failures and len(self._failures) + len(self._blocked_until) > MAX_TRACKED:
            del self._failures[next(iter(self._failures))]  # oldest first; blocks are kept
