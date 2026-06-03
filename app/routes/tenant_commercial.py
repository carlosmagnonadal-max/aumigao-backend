from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.tenant_commercial import TenantCommercialPlansResponse, TenantCommercialRuntimeResponse
from app.services.tenant_commercial_service import get_commercial_plans, get_tenant_commercial_runtime


router = APIRouter(prefix="/tenants", tags=["tenant-commercial"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-commercial"])


@router.get("/commercial/plans", response_model=TenantCommercialPlansResponse)
@api_router.get("/commercial/plans", response_model=TenantCommercialPlansResponse)
def list_commercial_plans():
    return get_commercial_plans()


@router.get("/current/commercial-runtime", response_model=TenantCommercialRuntimeResponse)
@api_router.get("/current/commercial-runtime", response_model=TenantCommercialRuntimeResponse)
def get_current_commercial_runtime(request: Request, db: Session = Depends(get_db)):
    return get_tenant_commercial_runtime(db, tenant_id=getattr(request.state, "tenant_id", None))


@router.get("/{tenant_id}/commercial-runtime", response_model=TenantCommercialRuntimeResponse)
@api_router.get("/{tenant_id}/commercial-runtime", response_model=TenantCommercialRuntimeResponse)
def get_commercial_runtime(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_commercial_runtime(db, tenant_id=tenant_id)
