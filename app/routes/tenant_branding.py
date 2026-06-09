from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.dependencies.rbac import require_permission
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
    db: Session = Depends(get_db),
):
    tenant = resolve_current_tenant(db, request)
    return update_tenant_branding_runtime(db, tenant, payload)
