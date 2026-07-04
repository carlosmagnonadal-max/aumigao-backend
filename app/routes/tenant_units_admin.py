"""tenant_units_admin.py — CRUD self-service de unidades do tenant.

Familia self-service (admin do proprio tenant):
  GET   /api/admin/tenants/current/units      — lista + cap
  POST  /api/admin/tenants/current/units      — cria unidade
  PATCH /api/admin/tenants/current/units/{unit_id} — renomeia / ativa / desativa

Pares sem /api (retrocompatibilidade de rota):
  GET   /admin/tenants/current/units
  POST  /admin/tenants/current/units
  PATCH /admin/tenants/current/units/{unit_id}

RBAC:
  Leitura  → units.read
  Escrita  → units.update

Regra de ouro: todo endpoint de ESCRITA admin chama get_admin_tenant_scope
para injetar o GUC RLS antes de qualquer INSERT/UPDATE.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope, is_super_admin
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant_units_admin import (
    TenantUnitCreatePayload,
    TenantUnitPatchPayload,
    TenantUnitsAdminListResponse,
)
from app.services.tenant_units_admin_service import create_unit, list_units, patch_unit

# ── routers ─────────────────────────────────────────────────────────────────
# Par sem /api (rota amigável sem prefixo de API)
admin_router = APIRouter(
    prefix="/admin/tenants",
    tags=["admin-tenant-units"],
    dependencies=[Depends(require_permission("units.read"))],
)

# Par com /api (padrão canônico)
admin_api_router = APIRouter(
    prefix="/api/admin/tenants",
    tags=["admin-tenant-units"],
    dependencies=[Depends(require_permission("units.read"))],
)


def _resolve_own_tenant(admin: User, db: Session) -> Tenant:
    """Resolve o tenant do admin autenticado (NUNCA permite cross-tenant)."""
    if not is_super_admin(admin):
        if not admin.tenant_id:
            raise HTTPException(status_code=400, detail="Admin sem tenant_id configurado")
        tenant = db.get(Tenant, admin.tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant não encontrado")
        return tenant
    # super_admin em modo act-as segue o tenant do ato; sem act-as → erro explicativo
    scope = get_admin_tenant_scope(admin, db)
    if scope.tenant_id:
        tenant = db.get(Tenant, scope.tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant não encontrado")
        return tenant
    raise HTTPException(
        status_code=400,
        detail="super_admin: use X-Act-As-Tenant-Id para operar em um tenant específico.",
    )


# ── GET /current/units ───────────────────────────────────────────────────────
@admin_router.get("/current/units", response_model=TenantUnitsAdminListResponse)
@admin_api_router.get("/current/units", response_model=TenantUnitsAdminListResponse)
def list_tenant_units(
    admin: User = Depends(require_permission("units.read")),
    db: Session = Depends(get_db),
):
    get_admin_tenant_scope(admin, db)
    tenant = _resolve_own_tenant(admin, db)
    return list_units(tenant, db)


# ── POST /current/units ──────────────────────────────────────────────────────
@admin_router.post("/current/units", status_code=201)
@admin_api_router.post("/current/units", status_code=201)
def create_tenant_unit(
    payload: TenantUnitCreatePayload,
    admin: User = Depends(require_permission("units.update")),
    db: Session = Depends(get_db),
):
    get_admin_tenant_scope(admin, db)
    tenant = _resolve_own_tenant(admin, db)
    return create_unit(tenant, db, name=payload.name, actor=admin)


# ── PATCH /current/units/{unit_id} ──────────────────────────────────────────
@admin_router.patch("/current/units/{unit_id}")
@admin_api_router.patch("/current/units/{unit_id}")
def patch_tenant_unit(
    unit_id: str,
    payload: TenantUnitPatchPayload,
    admin: User = Depends(require_permission("units.update")),
    db: Session = Depends(get_db),
):
    get_admin_tenant_scope(admin, db)
    tenant = _resolve_own_tenant(admin, db)
    return patch_unit(tenant, db, unit_id=unit_id, name=payload.name, enabled=payload.enabled, actor=admin)
