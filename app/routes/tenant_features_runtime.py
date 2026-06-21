from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import ensure_tenant_access, get_admin_tenant_scope
from app.models.user import User
from app.schemas.tenant_feature_runtime import TenantFeatureRuntimeResponse
from app.services.tenant_feature_runtime_service import get_tenant_feature_runtime


router = APIRouter(prefix="/tenants", tags=["tenant-features-runtime"])
api_router = APIRouter(prefix="/api/tenants", tags=["tenant-features-runtime"])


@router.get("/current/features-runtime", response_model=TenantFeatureRuntimeResponse)
@api_router.get("/current/features-runtime", response_model=TenantFeatureRuntimeResponse)
def get_current_features_runtime(request: Request, db: Session = Depends(get_db)):
    # Público por desenho: escopado ao tenant da PRÓPRIA requisição (resolvido pelo
    # middleware via subdomínio/header). Usado pelo app e pelo admin para branding.
    return get_tenant_feature_runtime(db, tenant_id=getattr(request.state, "tenant_id", None))


@router.get("/{tenant_id}/features-runtime", response_model=TenantFeatureRuntimeResponse)
@api_router.get("/{tenant_id}/features-runtime", response_model=TenantFeatureRuntimeResponse)
def get_features_runtime(
    tenant_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Lookup por ID/slug é superfície de ADMIN — exige autenticação e escopo de
    # tenant (super_admin vê qualquer um; admin só o próprio tenant). Nenhum cliente
    # usa esta rota (app e admin usam /current). Onda 1 / mt-MT2 / crítico C08.
    scope = get_admin_tenant_scope(user, db)
    result = get_tenant_feature_runtime(db, tenant_id=tenant_id)
    resolved_tenant_id = getattr(result, "tenant_id", None)
    if resolved_tenant_id is None and isinstance(result, dict):
        resolved_tenant_id = result.get("tenant_id")
    ensure_tenant_access(resolved_tenant_id, scope)
    return result
