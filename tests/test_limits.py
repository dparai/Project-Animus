from app import limits
from app.limits import LoginLimiter


class FakeClock:
    def __init__(self, t=500.0):
        self.t = t

    def __call__(self):
        return self.t


def test_not_blocked_until_fifth_failure():
    clock = FakeClock()
    lim = LoginLimiter(clock=clock)
    for _ in range(4):
        lim.fail("a")
        assert lim.blocked("a") == 0
    lim.fail("a")
    assert lim.blocked("a") == 900


def test_block_counts_down_and_expires():
    clock = FakeClock()
    lim = LoginLimiter(clock=clock)
    for _ in range(5):
        lim.fail("a")
    clock.t += 450.4
    assert lim.blocked("a") == 450
    clock.t += 449.5
    assert lim.blocked("a") == 1
    clock.t += 1
    assert lim.blocked("a") == 0
    lim.fail("a")  # a fresh count after the block ends
    assert lim.blocked("a") == 0


def test_failures_during_block_do_not_extend_it():
    clock = FakeClock()
    lim = LoginLimiter(clock=clock)
    for _ in range(5):
        lim.fail("a")
    clock.t += 600
    for _ in range(10):
        lim.fail("a")
    assert lim.blocked("a") == 300


def test_old_failures_fall_out_of_the_window():
    clock = FakeClock()
    lim = LoginLimiter(clock=clock)
    for _ in range(4):
        lim.fail("a")
    clock.t += 900
    lim.fail("a")
    assert lim.blocked("a") == 0


def test_clear_and_ips_are_independent():
    clock = FakeClock()
    lim = LoginLimiter(max_failures=2, window_s=60, clock=clock)
    lim.fail("a")
    lim.clear("a")
    lim.fail("a")
    assert lim.blocked("a") == 0
    lim.fail("b")
    lim.fail("b")
    assert lim.blocked("b") == 60 and lim.blocked("a") == 0


def test_memory_stays_bounded(monkeypatch):
    monkeypatch.setattr(limits, "MAX_TRACKED", 50)
    clock = FakeClock()
    lim = LoginLimiter(clock=clock)
    for i in range(500):
        lim.fail(f"10.0.{i // 256}.{i % 256}")
    assert len(lim._failures) + len(lim._blocked_until) <= 50


def test_reserve_counts_checks_in_flight():
    clock = FakeClock()
    lim = LoginLimiter(clock=clock)
    assert all(lim.reserve("a") for _ in range(5))
    assert not lim.reserve("a")  # five checks already in flight
    lim.release("a")
    lim.fail("a")  # that check failed: 1 failure + 4 in flight
    assert not lim.reserve("a")
    for _ in range(4):
        lim.release("a")
    assert lim.reserve("a") and lim._in_flight == {"a": 1}
    lim.release("a")
    assert lim._in_flight == {}
    assert lim.reserve("b")


def test_clear_never_lifts_a_block():
    clock = FakeClock()
    lim = LoginLimiter(clock=clock)
    for _ in range(5):
        lim.fail("a")
    lim.clear("a")
    assert lim.blocked("a") == 900
    assert not lim.reserve("a")
