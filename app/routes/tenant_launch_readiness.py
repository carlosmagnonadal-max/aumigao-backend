from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import ensure_tenant_access, get_admin_tenant_scope, is_super_admin
from app.models.user import User
from app.schemas.tenant_launch_readiness import TenantLaunchReadinessResponse
from app.services.tenant_launch_readiness_service import get_tenant_launch_readiness


# Prontidao de lancamento expoe branding/plano/billing/unidades/score do tenant —
# dado de gestao; exige acesso admin (antes estava publico, sem auth).
router = APIRouter(prefix="/tenants", tags=["tenant-launch-readiness"], dependencies=[Depends(require_permission("admin.access"))])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-launch-readiness"], dependencies=[Depends(require_permission("admin.access"))])


@router.get("/current/launch-readiness", response_model=TenantLaunchReadinessResponse)
@api_router.get("/current/launch-readiness", response_model=TenantLaunchReadinessResponse)
def get_current_launch_readiness(
    request: Request,
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    # Admin de tenant: "current" = o PROPRIO tenant do usuario autenticado (o BFF do
    # admin-web nao injeta X-Tenant-Slug, entao o resolver cairia no tenant default).
    # super_admin mantem o comportamento original (resolucao pela requisicao).
    if not is_super_admin(admin):
        scope = get_admin_tenant_scope(admin, db)
        return get_tenant_launch_readiness(db, tenant_id=scope.tenant_id)
    return get_tenant_launch_readiness(db, request=request)


@router.get("/{tenant_id}/launch-readiness", response_model=TenantLaunchReadinessResponse)
@api_router.get("/{tenant_id}/launch-readiness", response_model=TenantLaunchReadinessResponse)
def get_launch_readiness(
    tenant_id: str,
    admin: User = Depends(require_permission("admin.access")),
    db: Session = Depends(get_db),
):
    # Isolamento multi-tenant: admin de tenant so le a prontidao do PROPRIO tenant;
    # cross-tenant -> 404 (nao vaza existencia). super_admin acessa qualquer tenant.
    if not is_super_admin(admin):
        ensure_tenant_access(tenant_id, get_admin_tenant_scope(admin, db))
    return get_tenant_launch_readiness(db, tenant_id=tenant_id)
