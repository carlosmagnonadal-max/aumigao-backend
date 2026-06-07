from __future__ import annotations

import time
from collections.abc import Callable


def normalize_login_identifier(email: str) -> str:
    return str(email or "").strip().lower()


class InMemoryLoginRateLimiter:
    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 600,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.clock = clock
        self._failures: dict[str, list[float]] = {}

    def _prune_expired_keys(self, now: float) -> None:
        for key, timestamps in list(self._failures.items()):
            recent = [timestamp for timestamp in timestamps if now - timestamp <= self.window_seconds]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)

    def _recent_failures(self, identifier: str, now: float | None = None) -> list[float]:
        key = normalize_login_identifier(identifier)
        current = self.clock() if now is None else now
        self._prune_expired_keys(current)
        return self._failures.get(key, [])

    def is_blocked(self, identifier: str) -> bool:
        return len(self._recent_failures(identifier)) >= self.max_failures

    def record_failure(self, identifier: str) -> int:
        key = normalize_login_identifier(identifier)
        now = self.clock()
        recent = self._recent_failures(key, now)
        recent.append(now)
        self._failures[key] = recent
        return len(recent)

    def clear(self, identifier: str) -> None:
        self._failures.pop(normalize_login_identifier(identifier), None)


# Beta-only limiter: process-local, suitable for the current single Railway replica.
login_rate_limiter = InMemoryLoginRateLimiter()
