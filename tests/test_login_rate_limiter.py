from app.services.login_rate_limiter import (
    InMemoryLoginRateLimiter,
    normalize_login_identifier,
)


class FakeClock:
    """Controllable monotonic clock for deterministic window tests."""

    def __init__(self, now: float = 0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# normalize_login_identifier
# ---------------------------------------------------------------------------

def test_normalize_strips_and_lowercases():
    assert normalize_login_identifier("  User@Example.COM  ") == "user@example.com"


def test_normalize_none_returns_empty_string():
    assert normalize_login_identifier(None) == ""


def test_normalize_empty_string():
    assert normalize_login_identifier("") == ""


def test_normalize_only_whitespace():
    assert normalize_login_identifier("   ") == ""


# ---------------------------------------------------------------------------
# record_failure
# ---------------------------------------------------------------------------

def test_record_failure_returns_running_count():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    assert limiter.record_failure("a@b.com") == 1
    assert limiter.record_failure("a@b.com") == 2
    assert limiter.record_failure("a@b.com") == 3


def test_record_failure_normalizes_identifier():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    limiter.record_failure("  A@B.com ")
    # different surface form, same normalized key -> count continues
    assert limiter.record_failure("a@b.com") == 2


def test_record_failure_separate_identifiers_independent():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    assert limiter.record_failure("one@x.com") == 1
    assert limiter.record_failure("two@x.com") == 1
    assert limiter.record_failure("one@x.com") == 2


# ---------------------------------------------------------------------------
# is_blocked after max_failures
# ---------------------------------------------------------------------------

def test_five_failures_are_allowed_and_recorded_before_blocking():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    email = "cliente@example.com"
    for expected_count in range(1, 6):
        assert limiter.is_blocked(email) is False
        assert limiter.record_failure(email) == expected_count
    assert limiter.is_blocked(email) is True


def test_not_blocked_below_threshold():
    limiter = InMemoryLoginRateLimiter(max_failures=5, clock=FakeClock())
    for _ in range(4):
        limiter.record_failure("u@x.com")
    assert limiter.is_blocked("u@x.com") is False


def test_blocked_at_exactly_max_failures():
    limiter = InMemoryLoginRateLimiter(max_failures=5, clock=FakeClock())
    for _ in range(5):
        limiter.record_failure("u@x.com")
    assert limiter.is_blocked("u@x.com") is True


def test_blocked_above_max_failures():
    limiter = InMemoryLoginRateLimiter(max_failures=3, clock=FakeClock())
    for _ in range(6):
        limiter.record_failure("u@x.com")
    assert limiter.is_blocked("u@x.com") is True


def test_custom_max_failures_threshold():
    limiter = InMemoryLoginRateLimiter(max_failures=2, clock=FakeClock())
    assert limiter.record_failure("u@x.com") == 1
    assert limiter.is_blocked("u@x.com") is False
    limiter.record_failure("u@x.com")
    assert limiter.is_blocked("u@x.com") is True


def test_unknown_identifier_not_blocked():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    assert limiter.is_blocked("never-seen@x.com") is False


def test_is_blocked_uses_normalized_identifier():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    for _ in range(5):
        limiter.record_failure("  Cliente@Example.COM  ")
    assert limiter.is_blocked("cliente@example.com") is True
    assert limiter.is_blocked(" CLIENTE@example.com ") is True


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------

def test_clear_resets_failures():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    email = "cliente@example.com"
    for _ in range(5):
        limiter.record_failure(email)
    limiter.clear(email)
    assert limiter.is_blocked(email) is False
    assert limiter.record_failure(email) == 1


def test_clear_normalizes_identifier():
    limiter = InMemoryLoginRateLimiter(max_failures=1, clock=FakeClock())
    limiter.record_failure("u@x.com")
    limiter.clear("  U@X.COM ")
    assert limiter.is_blocked("u@x.com") is False


def test_clear_unknown_identifier_is_noop():
    limiter = InMemoryLoginRateLimiter(clock=FakeClock())
    # should not raise
    limiter.clear("nobody@x.com")


# ---------------------------------------------------------------------------
# window expiration (window_seconds)
# ---------------------------------------------------------------------------

def test_failures_expire_after_window():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(clock=clock)
    email = "cliente@example.com"
    for _ in range(5):
        limiter.record_failure(email)
    assert limiter.is_blocked(email) is True
    clock.advance(601)  # past default 600s window
    assert limiter.is_blocked(email) is False
    assert limiter.record_failure(email) == 1


def test_failure_at_exact_window_boundary_still_counts():
    # _prune keeps timestamps where now - ts <= window_seconds (inclusive)
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(max_failures=1, window_seconds=600, clock=clock)
    limiter.record_failure("u@x.com")
    clock.advance(600)  # exactly at boundary -> still within window
    assert limiter.is_blocked("u@x.com") is True
    clock.advance(0.0001)  # just past boundary
    assert limiter.is_blocked("u@x.com") is False


def test_sliding_window_partial_expiry():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(max_failures=3, window_seconds=100, clock=clock)
    limiter.record_failure("u@x.com")  # t=0
    clock.advance(50)
    limiter.record_failure("u@x.com")  # t=50
    clock.advance(50)
    limiter.record_failure("u@x.com")  # t=100
    # now=100: first failure (t=0) at boundary (100-0=100 <= 100) -> all 3 kept
    assert limiter.is_blocked("u@x.com") is True
    clock.advance(1)  # now=101: t=0 expires (101>100), two remain
    assert limiter.is_blocked("u@x.com") is False
    assert len(limiter._recent_failures("u@x.com")) == 2


def test_prune_removes_key_entirely_when_all_expired():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(window_seconds=10, clock=clock)
    limiter.record_failure("gone@x.com")
    clock.advance(20)
    # trigger a prune via a lookup on another key
    limiter._recent_failures("other@x.com")
    assert "gone@x.com" not in limiter._failures


# ---------------------------------------------------------------------------
# default (real) clock smoke
# ---------------------------------------------------------------------------

def test_default_clock_accumulates_without_expiring():
    limiter = InMemoryLoginRateLimiter(max_failures=3, window_seconds=600)
    assert limiter.record_failure("u@x.com") == 1
    assert limiter.record_failure("u@x.com") == 2
    assert limiter.is_blocked("u@x.com") is False
