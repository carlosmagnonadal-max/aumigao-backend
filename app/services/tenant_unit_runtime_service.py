import re
import unicodedata
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantUnit
from app.services.tenant_context import get_default_tenant, resolve_current_tenant


def _slugify(value: str | None, fallback: str) -> str:
    source = (value or fallback or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", source).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or fallback


def _resolve_tenant(db: Session, tenant_id: str | None = None, tenant: Tenant | None = None) -> Tenant:
    if tenant:
        return tenant

    if tenant_id and tenant_id != "current":
        existing = db.get(Tenant, tenant_id)
        if existing:
            return existing

        existing = db.query(Tenant).filter(Tenant.slug == tenant_id).first()
        if existing:
            return existing

    return get_default_tenant(db)


def _unit_to_runtime(unit: TenantUnit) -> dict[str, str | bool]:
    return {
        "id": unit.id,
        "name": unit.name,
        "slug": _slugify(unit.name, unit.id),
        "enabled": unit.status == "active",
    }


def get_default_units_runtime(tenant_id: str) -> dict[str, str | list[dict[str, Any]]]:
    return {
        "tenant_id": tenant_id,
        "units": [],
    }


def get_tenant_units_runtime(
    db: Session,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
) -> dict[str, str | list[dict[str, Any]]]:
    resolved_tenant = _resolve_tenant(db, tenant_id, tenant)
    units = (
        db.query(TenantUnit)
        .filter(TenantUnit.tenant_id == resolved_tenant.id)
        .order_by(TenantUnit.created_at.asc())
        .all()
    )

    return {
        "tenant_id": resolved_tenant.id,
        "units": [_unit_to_runtime(unit) for unit in units],
    }


def get_current_tenant_units_runtime(
    db: Session,
    request: Request | None = None,
) -> dict[str, str | list[dict[str, Any]]]:
    tenant = resolve_current_tenant(db, request)
    return get_tenant_units_runtime(db, tenant=tenant)


def get_unit_by_slug(
    db: Session,
    unit_slug: str,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
) -> dict[str, str | bool] | None:
    normalized_slug = (unit_slug or "").strip()
    if not normalized_slug:
        return None

    runtime = get_tenant_units_runtime(db, tenant_id=tenant_id, tenant=tenant)
    units = runtime.get("units", [])
    if not isinstance(units, list):
        return None

    for unit in units:
        if isinstance(unit, dict) and unit.get("slug") == normalized_slug:
            return unit

    return None


def is_unit_enabled(
    db: Session,
    unit_slug: str,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
) -> bool:
    unit = get_unit_by_slug(db, unit_slug, tenant_id=tenant_id, tenant=tenant)
    return bool(unit and unit.get("enabled"))


def resolve_current_unit(
    db: Session,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
    unit_slug: str | None = None,
) -> dict[str, str | bool] | None:
    if unit_slug:
        return get_unit_by_slug(db, unit_slug, tenant_id=tenant_id, tenant=tenant)

    runtime = get_tenant_units_runtime(db, tenant_id=tenant_id, tenant=tenant)
    units = runtime.get("units", [])
    if not isinstance(units, list):
        return None

    for unit in units:
        if isinstance(unit, dict) and unit.get("enabled"):
            return unit

    return units[0] if units and isinstance(units[0], dict) else None
