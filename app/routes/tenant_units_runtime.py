from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.tenant_unit_runtime import TenantUnitRuntimeResponse
from app.services.tenant_unit_runtime_service import get_current_tenant_units_runtime, get_tenant_units_runtime


router = APIRouter(prefix="/tenants", tags=["tenant-units-runtime"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-units-runtime"])


@router.get("/current/units-runtime", response_model=TenantUnitRuntimeResponse)
@api_router.get("/current/units-runtime", response_model=TenantUnitRuntimeResponse)
def get_current_units_runtime(request: Request, db: Session = Depends(get_db)):
    return get_current_tenant_units_runtime(db, request)


@router.get("/{tenant_id}/units-runtime", response_model=TenantUnitRuntimeResponse)
@api_router.get("/{tenant_id}/units-runtime", response_model=TenantUnitRuntimeResponse)
def get_units_runtime(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_units_runtime(db, tenant_id=tenant_id)
