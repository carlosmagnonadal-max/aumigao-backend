from __future__ import annotations

import os

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.services.tenant_context import get_default_tenant


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
        tenant = db.query(Tenant).filter(Tenant.slug == tenant_slug.lower()).first()
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
    return get_default_tenant(db)
