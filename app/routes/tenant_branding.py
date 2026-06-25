from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import get_admin_tenant_scope, is_super_admin
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant_branding import TenantBrandingRuntimeResponse
from app.schemas.tenant_branding_update import TenantBrandingUpdatePayload
from app.services.tenant_branding_service import get_tenant_branding_runtime, update_tenant_branding_runtime
from app.services.tenant_context import resolve_current_tenant


router = APIRouter(prefix="/tenants", tags=["tenant-branding"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-branding"])
admin_api_router = APIRouter(prefix="/api/admin/tenants", tags=["admin-tenant-branding"], dependencies=[Depends(require_permission("branding.read"))])


@router.get("/current/branding-runtime", response_model=TenantBrandingRuntimeResponse)
@api_router.get("/current/branding-runtime", response_model=TenantBrandingRuntimeResponse)
def get_current_branding_runtime(request: Request, db: Session = Depends(get_db)):
    return get_tenant_branding_runtime(db, getattr(request.state, "tenant_id", None))


@router.get("/{tenant_id}/branding-runtime", response_model=TenantBrandingRuntimeResponse)
@api_router.get("/{tenant_id}/branding-runtime", response_model=TenantBrandingRuntimeResponse)
def get_branding_runtime(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_branding_runtime(db, tenant_id)


@admin_api_router.patch("/current/branding", response_model=TenantBrandingRuntimeResponse)
def update_current_branding(
    payload: TenantBrandingUpdatePayload,
    request: Request,
    admin: User = Depends(require_permission("branding.update")),
    db: Session = Depends(get_db),
):
    # A5: admin de tenant deve editar o branding do SEU tenant, não o default.
    # super_admin mantém o comportamento original (resolve_current_tenant / act-as).
    # Injeta o escopo RLS (super_admin → '*'; admin → próprio tenant) ANTES de
    # ler/gravar tenant_branding — senão o UPDATE viola WITH CHECK (o GUC ficaria
    # no tenant default, pois o BFF do admin-web não injeta X-Tenant-Slug).
    get_admin_tenant_scope(admin, db)
    if not is_super_admin(admin):
        # admin de tenant: usa o tenant_id do usuário autenticado
        if not admin.tenant_id:
            raise HTTPException(status_code=400, detail="Admin sem tenant_id configurado")
        tenant = db.get(Tenant, admin.tenant_id)
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant do admin nao encontrado")
    else:
        tenant = resolve_current_tenant(db, request)
    return update_tenant_branding_runtime(db, tenant, payload, actor=admin)
