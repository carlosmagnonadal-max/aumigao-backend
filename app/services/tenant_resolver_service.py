from __future__ import annotations

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


def resolve_tenant_from_request(request: Request, db: Session) -> Tenant:
    return (
        resolve_tenant_from_headers(request, db)
        or resolve_tenant_from_host(request, db)
        or get_default_tenant(db)
    )
