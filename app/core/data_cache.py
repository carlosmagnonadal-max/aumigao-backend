"""Cache de dados de leitura — camada entre a API e o banco.

Serve respostas quentes (ex.: app-config do tenant) sem bater no Postgres a
cada request. Espelha o padrão do RedisRateLimiter (login_rate_limiter.py):

- Cliente Redis LAZY via UPSTASH_REDIS_URL (mesma instância já usada pelo
  rate limit de login) — nunca conecta no import.
- Sem UPSTASH_REDIS_URL, opera 100% com o fallback in-memory por processo.
- FAIL-OPEN: nenhum erro de cache jamais propaga para o request; no pior
  caso a consulta vai ao banco, exatamente como antes do cache existir.

Kill switch: DATA_CACHE_ENABLED=false desliga tudo em runtime (get vira miss
permanente, set/delete viram no-op de escrita — delete ainda limpa).
"""

import json
import logging
import os
import threading
import time

_logger = logging.getLogger("app.data_cache")


def _cache_enabled() -> bool:
    return (os.getenv("DATA_CACHE_ENABLED") or "true").strip().lower() not in {"0", "false", "off", "no"}


class InMemoryTTLCache:
    """Fallback local por processo. Thread-safe, expiração preguiçosa no get."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.monotonic() >= expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value, ttl_seconds: float) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + float(ttl_seconds), value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class DataCache:
    """Cache JSON com TTL: Redis quando disponível, in-memory como fallback.

    Quando o Redis está acessível, ele é a fonte autoritativa (um miss no
    Redis NÃO consulta o fallback — evita servir dado velho de outra época
    de outage). O fallback só é escrito/lido quando o Redis falha ou não
    está configurado.
    """

    def __init__(self, redis_url: str | None = None, key_prefix: str = "dc") -> None:
        self._redis_url = (redis_url if redis_url is not None else os.getenv("UPSTASH_REDIS_URL", "")).strip()
        self._key_prefix = key_prefix
        self._client = None  # lazy — jamais conectar no import
        self._fallback = InMemoryTTLCache()

    def _get_client(self):
        if self._client is None:
            import redis as redis_lib  # import local p/ não falhar sem o pacote

            self._client = redis_lib.from_url(
                self._redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
                max_connections=10,
            )
        return self._client

    def _key(self, key: str) -> str:
        return f"{self._key_prefix}:{key}"

    def get_json(self, key: str):
        """Retorna o valor cacheado (estruturas JSON) ou None em miss/erro/desligado."""
        if not _cache_enabled():
            return None
        full = self._key(key)
        if self._redis_url:
            try:
                raw = self._get_client().get(full)
                return json.loads(raw) if raw is not None else None
            except Exception as exc:
                _logger.warning("data_cache get falhou (%s), usando fallback in-memory.", exc)
        return self._fallback.get(full)

    def set_json(self, key: str, value, ttl_seconds: int) -> None:
        """Grava o valor com TTL. Silencioso em erro (fail-open)."""
        if not _cache_enabled():
            return
        full = self._key(key)
        try:
            payload = json.dumps(value, default=str)
        except Exception as exc:
            _logger.warning("data_cache set: valor nao serializavel (%s) — ignorando.", exc)
            return
        if self._redis_url:
            try:
                self._get_client().set(full, payload, ex=int(ttl_seconds))
                return
            except Exception as exc:
                _logger.warning("data_cache set falhou (%s), usando fallback in-memory.", exc)
        # round-trip pelo JSON: o fallback serve exatamente o que o Redis serviria
        self._fallback.set(full, json.loads(payload), float(ttl_seconds))

    def delete(self, key: str) -> None:
        """Invalidação explícita. Limpa Redis E fallback; nunca propaga erro."""
        full = self._key(key)
        if self._redis_url:
            try:
                self._get_client().delete(full)
            except Exception as exc:
                _logger.warning("data_cache delete falhou (%s).", exc)
        self._fallback.delete(full)


# Singleton do processo — mesmo padrão do login_rate_limiter.
data_cache = DataCache()
