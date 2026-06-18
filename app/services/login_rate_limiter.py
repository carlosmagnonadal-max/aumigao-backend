from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

_logger = logging.getLogger("aumigao.rate_limiter")


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


class RedisRateLimiter:
    """Rate limiter centralizado usando Redis (Upstash).

    Usa janela fixa via INCR + EXPIRE atômico (pipeline).
    Chaves prefixadas: ``rl:<prefix>:<identifier>``.

    Se o Redis estiver indisponível em runtime, cada operação faz fallback
    silencioso para o ``_fallback`` in-memory — NUNCA propaga exceção para
    o caller, garantindo que uma falha no Redis jamais bloqueie o login.

    Conexão lazy: o cliente Redis só é criado na primeira chamada, nunca no
    import-time. Assim, a ausência de UPSTASH_REDIS_URL não causa erro ao
    importar o módulo.
    """

    def __init__(
        self,
        redis_url: str,
        max_failures: int = 5,
        window_seconds: int = 600,
        key_prefix: str = "login",
    ):
        self._redis_url = redis_url
        self.max_failures = max_failures
        self.window_seconds = int(window_seconds)
        self._key_prefix = key_prefix
        self._client = None  # lazy
        # Fallback in-memory com os mesmos parâmetros (mesmos limites)
        self._fallback = InMemoryLoginRateLimiter(
            max_failures=max_failures,
            window_seconds=float(window_seconds),
        )

    # ------------------------------------------------------------------
    # _failures: exposto para que o fixture de testes possa fazer .clear()
    # sem quebrar. Delega ao fallback in-memory.
    # ------------------------------------------------------------------
    @property
    def _failures(self) -> dict:
        return self._fallback._failures

    @_failures.setter
    def _failures(self, value: dict) -> None:
        self._fallback._failures = value

    # ------------------------------------------------------------------
    # Conexão lazy
    # ------------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            import redis as redis_lib  # importação local p/ não falhar sem pacote
            self._client = redis_lib.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                max_connections=10,
            )
        return self._client

    def _redis_key(self, identifier: str) -> str:
        normalized = normalize_login_identifier(identifier)
        return f"rl:{self._key_prefix}:{normalized}"

    # ------------------------------------------------------------------
    # Interface pública — mesma do InMemoryLoginRateLimiter
    # ------------------------------------------------------------------

    def is_blocked(self, identifier: str) -> bool:
        try:
            client = self._get_client()
            key = self._redis_key(identifier)
            raw = client.get(key)
            count = int(raw) if raw is not None else 0
            return count >= self.max_failures
        except Exception as exc:
            _logger.warning("Redis is_blocked falhou (%s), usando fallback in-memory.", exc)
            return self._fallback.is_blocked(identifier)

    def record_failure(self, identifier: str) -> int:
        try:
            client = self._get_client()
            key = self._redis_key(identifier)
            pipe = client.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.window_seconds, nx=True)  # só seta TTL se chave nova
            results = pipe.execute()
            return int(results[0])
        except Exception as exc:
            _logger.warning("Redis record_failure falhou (%s), usando fallback in-memory.", exc)
            return self._fallback.record_failure(identifier)

    def clear(self, identifier: str) -> None:
        try:
            client = self._get_client()
            key = self._redis_key(identifier)
            client.delete(key)
            # Limpa também o fallback (caso tenha acumulado entradas por falhas anteriores)
            self._fallback.clear(identifier)
        except Exception as exc:
            _logger.warning("Redis clear falhou (%s), usando fallback in-memory.", exc)
            self._fallback.clear(identifier)


# ---------------------------------------------------------------------------
# Factory: seleciona Redis ou in-memory baseado em UPSTASH_REDIS_URL.
# Chamada no nível de módulo — lazy e segura.
# ---------------------------------------------------------------------------

def _make_rate_limiter(
    max_failures: int = 5,
    window_seconds: float = 600,
    key_prefix: str = "login",
) -> InMemoryLoginRateLimiter | RedisRateLimiter:
    """Cria o limiter correto baseado em UPSTASH_REDIS_URL.

    - Var ausente (CI/local/testes sem Redis) → InMemoryLoginRateLimiter.
    - Var presente → RedisRateLimiter (com fallback automático se Redis cair).
    """
    redis_url = os.getenv("UPSTASH_REDIS_URL", "").strip()
    if not redis_url:
        return InMemoryLoginRateLimiter(
            max_failures=max_failures,
            window_seconds=window_seconds,
        )
    return RedisRateLimiter(
        redis_url=redis_url,
        max_failures=max_failures,
        window_seconds=int(window_seconds),
        key_prefix=key_prefix,
    )


# Singleton global — mantém retrocompatibilidade com imports existentes.
login_rate_limiter = _make_rate_limiter(max_failures=5, window_seconds=600, key_prefix="login")
