from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.tenant_branding import TenantBrandingRuntimeResponse
from app.services.tenant_branding_service import get_tenant_branding_runtime


router = APIRouter(prefix="/tenants", tags=["tenant-branding"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-branding"])


@router.get("/current/branding-runtime", response_model=TenantBrandingRuntimeResponse)
@api_router.get("/current/branding-runtime", response_model=TenantBrandingRuntimeResponse)
def get_current_branding_runtime(request: Request, db: Session = Depends(get_db)):
    return get_tenant_branding_runtime(db, getattr(request.state, "tenant_id", None))


@router.get("/{tenant_id}/branding-runtime", response_model=TenantBrandingRuntimeResponse)
@api_router.get("/{tenant_id}/branding-runtime", response_model=TenantBrandingRuntimeResponse)
def get_branding_runtime(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_branding_runtime(db, tenant_id)
