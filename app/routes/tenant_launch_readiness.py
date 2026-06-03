from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.tenant_launch_readiness import TenantLaunchReadinessResponse
from app.services.tenant_launch_readiness_service import get_tenant_launch_readiness


router = APIRouter(prefix="/tenants", tags=["tenant-launch-readiness"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-launch-readiness"])


@router.get("/current/launch-readiness", response_model=TenantLaunchReadinessResponse)
@api_router.get("/current/launch-readiness", response_model=TenantLaunchReadinessResponse)
def get_current_launch_readiness(request: Request, db: Session = Depends(get_db)):
    return get_tenant_launch_readiness(db, request=request)


@router.get("/{tenant_id}/launch-readiness", response_model=TenantLaunchReadinessResponse)
@api_router.get("/{tenant_id}/launch-readiness", response_model=TenantLaunchReadinessResponse)
def get_launch_readiness(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_launch_readiness(db, tenant_id=tenant_id)
