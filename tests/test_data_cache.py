"""Testes do cache de dados (app/core/data_cache.py) e do split de leitura.

Cobrem apenas o caminho in-memory (sem Redis) — o caminho Redis usa o mesmo
padrão fail-open já testado no rate limiter e não é exercitado no CI.
"""

import time

import pytest

from app.core import database
from app.core.data_cache import DataCache, InMemoryTTLCache


@pytest.fixture(autouse=True)
def _cache_ligado(monkeypatch):
    # O conftest desliga o cache pra suíte inteira (2d); estes testes exercitam
    # o próprio cache, então religam explicitamente.
    monkeypatch.setenv("DATA_CACHE_ENABLED", "true")


# ---------------------------------------------------------------------------
# InMemoryTTLCache
# ---------------------------------------------------------------------------

def test_inmemory_set_get_roundtrip():
    cache = InMemoryTTLCache()
    cache.set("k", {"a": 1}, ttl_seconds=60)
    assert cache.get("k") == {"a": 1}


def test_inmemory_miss_returns_none():
    assert InMemoryTTLCache().get("nope") is None


def test_inmemory_expira_apos_ttl(monkeypatch):
    cache = InMemoryTTLCache()
    now = time.monotonic()
    cache.set("k", "v", ttl_seconds=10)
    monkeypatch.setattr(time, "monotonic", lambda: now + 11)
    assert cache.get("k") is None


def test_inmemory_delete():
    cache = InMemoryTTLCache()
    cache.set("k", "v", ttl_seconds=60)
    cache.delete("k")
    assert cache.get("k") is None


# ---------------------------------------------------------------------------
# DataCache sem Redis (fallback in-memory)
# ---------------------------------------------------------------------------

def _cache_sem_redis() -> DataCache:
    return DataCache(redis_url="")


def test_datacache_roundtrip_json():
    cache = _cache_sem_redis()
    cache.set_json("app:t1", {"branding": {"logo_url": "x"}}, ttl_seconds=60)
    assert cache.get_json("app:t1") == {"branding": {"logo_url": "x"}}


def test_datacache_normaliza_como_json():
    # tuplas viram listas — o fallback serve exatamente o que o Redis serviria
    cache = _cache_sem_redis()
    cache.set_json("k", {"pair": (1, 2)}, ttl_seconds=60)
    assert cache.get_json("k") == {"pair": [1, 2]}


def test_datacache_delete_invalida():
    cache = _cache_sem_redis()
    cache.set_json("k", {"a": 1}, ttl_seconds=60)
    cache.delete("k")
    assert cache.get_json("k") is None


def test_datacache_valor_nao_serializavel_nao_propaga():
    cache = _cache_sem_redis()
    cache.set_json("k", {"engine": object()}, ttl_seconds=60)  # default=str resolve
    # objetos viram string via default=str — nunca levanta exceção
    assert isinstance(cache.get_json("k")["engine"], str)


def test_kill_switch_desliga_get_e_set(monkeypatch):
    monkeypatch.setenv("DATA_CACHE_ENABLED", "false")
    cache = _cache_sem_redis()
    cache.set_json("k", {"a": 1}, ttl_seconds=60)
    assert cache.get_json("k") is None


def test_kill_switch_default_ligado(monkeypatch):
    monkeypatch.delenv("DATA_CACHE_ENABLED", raising=False)
    cache = _cache_sem_redis()
    cache.set_json("k", {"a": 1}, ttl_seconds=60)
    assert cache.get_json("k") == {"a": 1}


# ---------------------------------------------------------------------------
# Invalidação do app-config
# ---------------------------------------------------------------------------

def test_invalidate_app_config_deleta_chave_do_tenant(monkeypatch):
    from app.services import tenant_app_config_service as svc

    deleted: list[str] = []
    monkeypatch.setattr(svc.data_cache, "delete", deleted.append)

    svc.invalidate_tenant_app_config_cache("tenant-x")
    assert deleted == [svc.app_config_cache_key("tenant-x")]


def test_invalidate_app_config_ignora_tenant_vazio(monkeypatch):
    from app.services import tenant_app_config_service as svc

    monkeypatch.setattr(
        svc.data_cache, "delete",
        lambda key: pytest.fail("delete não deveria ser chamado sem tenant_id"),
    )
    svc.invalidate_tenant_app_config_cache(None)
    svc.invalidate_tenant_app_config_cache("")


# ---------------------------------------------------------------------------
# Split leitura/escrita — sem READ_DATABASE_URL, réplica é alias do primary
# ---------------------------------------------------------------------------

def test_sem_read_url_read_engine_e_alias_do_primary():
    if database.READ_DATABASE_URL:
        pytest.skip("ambiente com READ_DATABASE_URL setada")
    assert database.read_engine is database.engine
    assert database.ReadSessionLocal is database.SessionLocal


def test_get_read_db_seta_rls_global_sem_request():
    gen = database.get_read_db()
    db = next(gen)
    try:
        assert db.info["rls_tenant"] == "*"
    finally:
        gen.close()
