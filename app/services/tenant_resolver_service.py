from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from weakref import WeakKeyDictionary

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantUnit
from app.services.tenant_context import get_default_tenant

logger = logging.getLogger(__name__)


def _clean_header(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def resolve_tenant_from_headers(request: Request, db: Session) -> Tenant | None:
    tenant_id = _clean_header(request.headers.get("X-Tenant-Id"))
    if tenant_id:
        tenant = db.get(Tenant, tenant_id)
        if tenant:
            return tenant

    tenant_slug = _clean_header(request.headers.get("X-Tenant-Slug"))
    if tenant_slug:
        normalized = tenant_slug.lower()
        # 1) Tentativa primária: slug do tenant raiz.
        tenant = db.query(Tenant).filter(Tenant.slug == normalized).first()
        if tenant:
            return tenant
        # 2) Fallback: slug de UNIDADE (TenantUnit.slug) — o app envia o slug da
        #    unidade recém-criada como X-Tenant-Slug; precisamos resolver o tenant PAI.
        #    Sem este fallback, um slug de unidade "não encontrado" faz o resolver cair
        #    no default ou retornar None → 400 TENANT_REQUIRED (BUG 3).
        unit = (
            db.query(TenantUnit)
            .filter(TenantUnit.slug == normalized, TenantUnit.status == "active")
            .first()
        )
        if unit:
            tenant = db.get(Tenant, unit.tenant_id)
            if tenant:
                return tenant

    return None


def extract_subdomain_from_host(host: str | None) -> str | None:
    if not host:
        return None

    hostname = host.split(":", 1)[0].strip().lower()
    if not hostname or hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return None

    parts = [part for part in hostname.split(".") if part]
    if len(parts) < 3:
        return None

    subdomain = parts[0].strip()
    return subdomain or None


def resolve_tenant_from_host(request: Request, db: Session) -> Tenant | None:
    subdomain = extract_subdomain_from_host(request.headers.get("host"))
    if not subdomain:
        return None

    return db.query(Tenant).filter(Tenant.slug == subdomain).first()


def _strict_tenant_resolution() -> bool:
    return os.getenv("STRICT_TENANT_RESOLUTION", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }


def resolve_tenant_from_request(request: Request, db: Session) -> Tenant | None:
    resolved = resolve_tenant_from_headers(request, db) or resolve_tenant_from_host(request, db)
    if resolved is not None:
        return resolved
    # Modo estrito (spec §6.4): sem fallback silencioso. A rota sensível deve exigir
    # tenant via require_tenant e receber 400 TENANT_REQUIRED. O default (beta) mantém
    # o tenant padrão para não quebrar requisições sem contexto de tenant.
    if _strict_tenant_resolution():
        return None
    path = getattr(request, "url", None)
    path_str = str(path.path) if path and hasattr(path, "path") else "<desconhecido>"
    logger.warning(
        "tenant_resolver.fallback_para_default path=%s host=%s",
        path_str,
        request.headers.get("host", "<sem-host>"),
    )
    return get_default_tenant(db)


# ---------------------------------------------------------------------------
# Cache de resolução de tenant
#
# Sem cache, TODA requisição abria uma sessão de banco e fazia 2-4 queries só
# para descobrir o tenant (quase sempre o default). Com o banco remoto, isso
# somava segundos de latência por requisição — inclusive em /health.
#
# O cache é chaveado pelo `session_factory` (via WeakKeyDictionary): em produção
# há um único SessionLocal, então o cache vale de verdade; em testes cada app
# cria o seu sessionmaker, garantindo isolamento entre testes (sem vazamento).
# Só cacheamos resoluções POSITIVAS, para não fixar um "tenant inexistente"
# logo antes de ele ser criado (ex.: onboarding de white-label).
# ---------------------------------------------------------------------------

_TENANT_CACHE_TTL_SECONDS = float(os.getenv("TENANT_CACHE_TTL_SECONDS", "300"))
_tenant_cache: "WeakKeyDictionary[object, dict[str, tuple[float, tuple[str, str]]]]" = (
    WeakKeyDictionary()
)
_tenant_cache_lock = threading.Lock()


def _tenant_cache_key(request: Request) -> str:
    tid = _clean_header(request.headers.get("X-Tenant-Id")) or ""
    tslug = (_clean_header(request.headers.get("X-Tenant-Slug")) or "").lower()
    host = (request.headers.get("host") or "").split(":", 1)[0].strip().lower()
    return f"{tid}|{tslug}|{host}"


def clear_tenant_cache() -> None:
    """Limpa o cache de resolução de tenant (chamar após criar/alterar tenant)."""
    with _tenant_cache_lock:
        _tenant_cache.clear()


def resolve_tenant_identity(
    request: Request, session_factory: Callable[[], Session]
) -> tuple[str, str] | None:
    """Retorna (tenant_id, tenant_slug) do request, com cache para evitar hit no banco
    a cada requisição. Só abre sessão de banco no cache-miss. Retorna None quando nenhum
    tenant é resolvido (modo estrito). O objeto Tenant não é exposto porque nada downstream
    o consome — apenas tenant_id/tenant_slug em request.state.
    """
    key = _tenant_cache_key(request)
    now = time.monotonic()

    if _TENANT_CACHE_TTL_SECONDS > 0:
        with _tenant_cache_lock:
            bucket = _tenant_cache.get(session_factory)
            if bucket is not None:
                hit = bucket.get(key)
                if hit is not None and (now - hit[0]) < _TENANT_CACHE_TTL_SECONDS:
                    return hit[1]

    with session_factory() as db:
        # Fase 2c: resolver de tenant lê a tabela tenants (sem tenant_id/RLS).
        # Define "*" por segurança — garante visibilidade mesmo após cutover.
        db.info["rls_tenant"] = "*"
        tenant = resolve_tenant_from_request(request, db)
        identity = (tenant.id, tenant.slug) if tenant else None

    if identity is not None and _TENANT_CACHE_TTL_SECONDS > 0:
        with _tenant_cache_lock:
            bucket = _tenant_cache.get(session_factory)
            if bucket is None:
                bucket = {}
                _tenant_cache[session_factory] = bucket
            bucket[key] = (now, identity)

    return identity
