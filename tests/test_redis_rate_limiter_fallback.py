"""Testes de fallback do RedisRateLimiter.

Verificam que, quando o Redis está indisponível (host inválido, timeout, etc.),
o limiter NÃO lança exceção e continua operando via in-memory fallback.

Não requerem Redis real: todos os cenários usam URLs inválidas.
"""
import os

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis_limiter(url: str = "redis://invalid-host-does-not-exist:6379/0", **kwargs):
    """Cria um RedisRateLimiter apontando para um host inexistente."""
    from app.services.login_rate_limiter import RedisRateLimiter
    return RedisRateLimiter(
        redis_url=url,
        max_failures=kwargs.get("max_failures", 3),
        window_seconds=kwargs.get("window_seconds", 60),
        key_prefix=kwargs.get("key_prefix", "test"),
    )


# ---------------------------------------------------------------------------
# Testes: operações NÃO devem estourar mesmo com Redis inválido
# ---------------------------------------------------------------------------

def test_redis_is_blocked_does_not_raise_on_invalid_host():
    """is_blocked com Redis inválido deve retornar False (fail-open) sem exceção."""
    limiter = _make_redis_limiter()
    result = limiter.is_blocked("user@test.com")
    assert result is False, "is_blocked deve retornar False no fallback (fail-open)"


def test_redis_record_failure_does_not_raise_on_invalid_host():
    """record_failure com Redis inválido deve retornar contagem do fallback sem exceção."""
    limiter = _make_redis_limiter()
    count = limiter.record_failure("user@test.com")
    assert isinstance(count, int), "record_failure deve retornar int mesmo no fallback"
    assert count == 1, "primeira falha no fallback deve retornar 1"


def test_redis_clear_does_not_raise_on_invalid_host():
    """clear com Redis inválido não deve lançar exceção."""
    limiter = _make_redis_limiter()
    limiter.clear("user@test.com")  # não deve levantar


def test_redis_fallback_accumulates_failures_in_memory():
    """Após Redis falhar, o fallback in-memory acumula corretamente."""
    limiter = _make_redis_limiter(max_failures=3)
    for i in range(1, 4):
        count = limiter.record_failure("blocked@test.com")
        assert count == i
    assert limiter.is_blocked("blocked@test.com") is True


def test_redis_clear_resets_fallback_state():
    """clear com Redis inválido ainda limpa o estado do fallback in-memory."""
    limiter = _make_redis_limiter(max_failures=2)
    limiter.record_failure("clr@test.com")
    limiter.record_failure("clr@test.com")
    assert limiter.is_blocked("clr@test.com") is True
    limiter.clear("clr@test.com")
    assert limiter.is_blocked("clr@test.com") is False


# ---------------------------------------------------------------------------
# Testes via factory (_make_rate_limiter) com UPSTASH_REDIS_URL inválida
# ---------------------------------------------------------------------------

def test_factory_with_invalid_url_uses_redis_limiter_with_fallback(monkeypatch):
    """Com UPSTASH_REDIS_URL inválida, factory retorna RedisRateLimiter e fallback funciona."""
    monkeypatch.setenv("UPSTASH_REDIS_URL", "redis://invalid-host-xyz:6379/0")

    # Reimporta a factory para pegar a env atualizada
    from app.services import login_rate_limiter as rl_module
    import importlib
    importlib.reload(rl_module)

    limiter = rl_module._make_rate_limiter(
        max_failures=5,
        window_seconds=600,
        key_prefix="factory_test",
    )

    from app.services.login_rate_limiter import RedisRateLimiter
    assert isinstance(limiter, RedisRateLimiter), "factory deve retornar RedisRateLimiter com URL setada"

    # Operações não devem lançar
    assert limiter.is_blocked("a@b.com") is False
    assert limiter.record_failure("a@b.com") == 1
    limiter.clear("a@b.com")


def test_factory_without_url_uses_in_memory(monkeypatch):
    """Sem UPSTASH_REDIS_URL, factory retorna InMemoryLoginRateLimiter."""
    monkeypatch.delenv("UPSTASH_REDIS_URL", raising=False)

    from app.services.login_rate_limiter import InMemoryLoginRateLimiter, _make_rate_limiter
    limiter = _make_rate_limiter(max_failures=5, window_seconds=600, key_prefix="noredis")
    assert isinstance(limiter, InMemoryLoginRateLimiter)

    # Operações funcionam normalmente
    assert limiter.is_blocked("x@y.com") is False
    assert limiter.record_failure("x@y.com") == 1
    limiter.clear("x@y.com")
    assert limiter.is_blocked("x@y.com") is False
