from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.tenant_app_config import TenantAppConfigResponse
from app.services.tenant_app_config_service import get_tenant_app_config


router = APIRouter(prefix="/tenants", tags=["tenant-app-config"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-app-config"])


@router.get("/current/app-config", response_model=TenantAppConfigResponse)
@api_router.get("/current/app-config", response_model=TenantAppConfigResponse)
def get_current_app_config(request: Request, db: Session = Depends(get_db)):
    return get_tenant_app_config(db, request=request)


@router.get("/{tenant_id}/app-config", response_model=TenantAppConfigResponse)
@api_router.get("/{tenant_id}/app-config", response_model=TenantAppConfigResponse)
def get_app_config(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_app_config(db, tenant_id=tenant_id)
