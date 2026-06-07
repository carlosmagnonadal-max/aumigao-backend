from app.services.login_rate_limiter import InMemoryLoginRateLimiter, normalize_login_identifier


class FakeClock:
    def __init__(self, now: float = 0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_five_failures_are_allowed_and_recorded_before_blocking():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(clock=clock)
    email = "cliente@example.com"

    for expected_count in range(1, 6):
        assert limiter.is_blocked(email) is False
        assert limiter.record_failure(email) == expected_count

    assert limiter.is_blocked(email) is True


def test_sixth_attempt_is_blocked_after_five_failures():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(clock=clock)
    email = "cliente@example.com"

    for _ in range(5):
        limiter.record_failure(email)

    assert limiter.is_blocked(email) is True


def test_successful_login_clears_counter():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(clock=clock)
    email = "cliente@example.com"

    for _ in range(5):
        limiter.record_failure(email)

    limiter.clear(email)

    assert limiter.is_blocked(email) is False
    assert limiter.record_failure(email) == 1


def test_window_expiration_allows_new_attempts():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(clock=clock)
    email = "cliente@example.com"

    for _ in range(5):
        limiter.record_failure(email)

    assert limiter.is_blocked(email) is True
    clock.advance(601)

    assert limiter.is_blocked(email) is False
    assert limiter.record_failure(email) == 1


def test_email_normalization_prevents_case_and_space_bypass():
    clock = FakeClock()
    limiter = InMemoryLoginRateLimiter(clock=clock)

    assert normalize_login_identifier("  Cliente@Example.COM  ") == "cliente@example.com"

    for _ in range(5):
        limiter.record_failure("  Cliente@Example.COM  ")

    assert limiter.is_blocked("cliente@example.com") is True
    assert limiter.is_blocked(" CLIENTE@example.com ") is True
