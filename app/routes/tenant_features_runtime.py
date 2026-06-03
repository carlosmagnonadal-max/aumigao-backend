from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.tenant_feature_runtime import TenantFeatureRuntimeResponse
from app.services.tenant_feature_runtime_service import get_tenant_feature_runtime


router = APIRouter(prefix="/tenants", tags=["tenant-features-runtime"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-features-runtime"])


@router.get("/current/features-runtime", response_model=TenantFeatureRuntimeResponse)
@api_router.get("/current/features-runtime", response_model=TenantFeatureRuntimeResponse)
def get_current_features_runtime(request: Request, db: Session = Depends(get_db)):
    return get_tenant_feature_runtime(db, tenant_id=getattr(request.state, "tenant_id", None))


@router.get("/{tenant_id}/features-runtime", response_model=TenantFeatureRuntimeResponse)
@api_router.get("/{tenant_id}/features-runtime", response_model=TenantFeatureRuntimeResponse)
def get_features_runtime(tenant_id: str, db: Session = Depends(get_db)):
    return get_tenant_feature_runtime(db, tenant_id=tenant_id)
