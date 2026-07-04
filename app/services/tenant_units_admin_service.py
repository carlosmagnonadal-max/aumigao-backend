"""Serviço self-service de unidades do tenant (CRUD admin).

Regras:
- `used` = contagem de unidades com status="active".
- cap = max_units_with_addon quando o tenant tiver o addon; caso contrário max_units.
  Para Enterprise (None), cap = None = ilimitado.
- Slug: derivado do name, único por tenant; sufixo -2/-3 em colisão.
- Desativar preserva o registro (status="inactive"); não deleta.
- Re-ativar também respeita o cap (ATIVAS >= max → 422).
"""
from __future__ import annotations

import re
import unicodedata

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantUnit


def _slugify(value: str, fallback: str = "unidade") -> str:
    source = (value or fallback).strip().lower()
    normalized = unicodedata.normalize("NFKD", source).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or fallback


def _unique_slug(db: Session, tenant_id: str, base_slug: str, exclude_id: str | None = None) -> str:
    """Garante unicidade do slug por tenant; sufixo -2/-3 em colisão."""
    slug = base_slug
    counter = 2
    while True:
        q = db.query(TenantUnit).filter(
            TenantUnit.tenant_id == tenant_id,
            TenantUnit.slug == slug,
        )
        if exclude_id:
            q = q.filter(TenantUnit.id != exclude_id)
        if not q.first():
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


def unit_to_dict(unit: TenantUnit) -> dict:
    return {
        "id": unit.id,
        "name": unit.name,
        "slug": unit.slug or _slugify(unit.name),
        "enabled": unit.status == "active",
        "created_at": unit.created_at,
    }


def _count_active(db: Session, tenant_id: str) -> int:
    return (
        db.query(TenantUnit)
        .filter(TenantUnit.tenant_id == tenant_id, TenantUnit.status == "active")
        .count()
    )


def _resolve_max_units(tenant: Tenant, db: Session) -> int | None:
    """Retorna o cap efetivo de unidades ATIVAS (None = ilimitado)."""
    from app.services.tenant_plan_service import get_tenant_capabilities
    caps = get_tenant_capabilities(tenant, db)
    # max_units_with_addon cobre o addon; se ausente cai em max_units.
    cap = caps.get("max_units_with_addon")
    if cap is None:
        cap = caps.get("max_units")
    return cap  # None = Enterprise ilimitado


def _enforce_cap(tenant: Tenant, db: Session) -> int | None:
    """Lança 422 com mensagem clara se ATIVAS >= cap. Retorna o cap."""
    cap = _resolve_max_units(tenant, db)
    if cap is None:
        return None  # ilimitado
    used = _count_active(db, tenant.id)
    if used >= cap:
        raise HTTPException(
            status_code=422,
            detail=f"Seu plano permite {cap} unidade(s). Desative uma unidade antes de criar outra.",
        )
    return cap


def list_units(tenant: Tenant, db: Session) -> dict:
    cap = _resolve_max_units(tenant, db)
    used = _count_active(db, tenant.id)
    units = (
        db.query(TenantUnit)
        .filter(TenantUnit.tenant_id == tenant.id)
        .order_by(TenantUnit.created_at.asc())
        .all()
    )
    return {
        "units": [unit_to_dict(u) for u in units],
        "max_units": cap,
        "used": used,
    }


def create_unit(tenant: Tenant, db: Session, name: str, actor=None) -> dict:
    _enforce_cap(tenant, db)

    base_slug = _slugify(name.strip())
    slug = _unique_slug(db, tenant.id, base_slug)

    unit = TenantUnit(
        tenant_id=tenant.id,
        name=name.strip(),
        slug=slug,
        status="active",
    )
    db.add(unit)

    try:
        from app.services.admin_operational_event_service import record_admin_operational_event
        record_admin_operational_event(
            db,
            event_type="created",
            entity_type="tenant_unit",
            entity_id=slug,
            title=f"Unidade criada: {name.strip()}",
            actor=actor,
            metadata={"name": name.strip(), "slug": slug, "tenant_id": tenant.id},
        )
    except Exception:
        pass

    db.commit()
    db.refresh(unit)
    return unit_to_dict(unit)


def patch_unit(tenant: Tenant, db: Session, unit_id: str, name: str | None, enabled: bool | None, actor=None) -> dict:
    unit = db.query(TenantUnit).filter(
        TenantUnit.id == unit_id,
        TenantUnit.tenant_id == tenant.id,
    ).first()
    if not unit:
        raise HTTPException(status_code=404, detail="Unidade não encontrada")

    changes: dict = {}

    if name is not None:
        new_name = name.strip()
        if new_name != unit.name:
            base_slug = _slugify(new_name)
            new_slug = _unique_slug(db, tenant.id, base_slug, exclude_id=unit.id)
            unit.name = new_name
            unit.slug = new_slug
            changes["name"] = new_name
            changes["slug"] = new_slug

    if enabled is not None:
        new_status = "active" if enabled else "inactive"
        if new_status != unit.status:
            if new_status == "active":
                # Re-ativar também respeita o cap
                cap = _resolve_max_units(tenant, db)
                if cap is not None:
                    used = _count_active(db, tenant.id)
                    if used >= cap:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Seu plano permite {cap} unidade(s). Desative uma unidade antes de reativar outra.",
                        )
            unit.status = new_status
            changes["status"] = new_status

    if changes:
        try:
            from app.services.admin_operational_event_service import record_admin_operational_event
            record_admin_operational_event(
                db,
                event_type="updated",
                entity_type="tenant_unit",
                entity_id=unit.id,
                title=f"Unidade atualizada: {unit.name}",
                actor=actor,
                metadata={"changes": changes, "tenant_id": tenant.id},
            )
        except Exception:
            pass

    db.commit()
    db.refresh(unit)
    return unit_to_dict(unit)
