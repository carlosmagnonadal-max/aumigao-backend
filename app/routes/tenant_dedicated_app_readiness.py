from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.tenant_dedicated_app_readiness import TenantDedicatedAppReadinessResponse
from app.services.tenant_dedicated_app_readiness_service import get_tenant_dedicated_app_readiness


router = APIRouter(prefix="/tenants", tags=["tenant-dedicated-app-readiness"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-dedicated-app-readiness"])


@router.get("/current/dedicated-app-readiness", response_model=TenantDedicatedAppReadinessResponse)
@api_router.get("/current/dedicated-app-readiness", response_model=TenantDedicatedAppReadinessResponse)
def get_current_dedicated_app_readiness(request: Request, db: Session = Depends(get_db)):
    return get_tenant_dedicated_app_readiness(db, request=request)


@router.get("/{tenant_id}/dedicated-app-readiness", response_model=TenantDedicatedAppReadinessResponse)
@api_router.get("/{tenant_id}/dedicated-app-readiness", response_model=TenantDedicatedAppReadinessResponse)
def get_dedicated_app_readiness(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_dedicated_app_readiness(db, tenant_id=tenant_id)
