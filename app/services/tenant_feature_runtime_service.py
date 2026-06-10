from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.services.tenant_context import get_default_tenant
from app.services.tenant_plan_service import FEATURE_CAPABILITY_KEYS, get_plan_capabilities, get_tenant_capabilities, tenant_has_feature


RUNTIME_FEATURE_KEYS = (
    "network_access",
    "dedicated_app",
    "custom_products",
    "custom_projects",
)

# Features de produto (flag direta por tenant, NAO gated por plano) expostas ao app
# para gatear exibicao. Ex.: verified_walkers controla a exibicao do selo de Confianca.
PRODUCT_RUNTIME_FEATURE_KEYS = (
    "verified_walkers",
)


def get_default_feature_runtime() -> dict[str, bool]:
    return {feature_key: False for feature_key in (*RUNTIME_FEATURE_KEYS, *PRODUCT_RUNTIME_FEATURE_KEYS)}


def _resolve_tenant(db: Session, tenant_id: str | None = None, tenant: Tenant | None = None) -> Tenant:
    if tenant:
        return tenant

    if tenant_id and tenant_id != "current":
        existing = db.get(Tenant, tenant_id)
        if existing:
            return existing

        existing = db.query(Tenant).filter(Tenant.slug == tenant_id).first()
        if existing:
            return existing

    return get_default_tenant(db)


def get_tenant_feature_runtime(
    db: Session,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
) -> dict[str, str | dict[str, bool]]:
    resolved_tenant = _resolve_tenant(db, tenant_id, tenant)
    base_capabilities = get_plan_capabilities(resolved_tenant.plan)
    tenant_capabilities = get_tenant_capabilities(resolved_tenant, db)

    features = get_default_feature_runtime()
    for feature_key in RUNTIME_FEATURE_KEYS:
        capability_key = FEATURE_CAPABILITY_KEYS.get(feature_key, feature_key)
        base_allows = bool(base_capabilities.get(capability_key))
        tenant_allows = bool(tenant_capabilities.get(capability_key))
        features[feature_key] = base_allows and tenant_allows

    for feature_key in PRODUCT_RUNTIME_FEATURE_KEYS:
        features[feature_key] = tenant_has_feature(resolved_tenant, db, feature_key)

    return {
        "tenant_id": resolved_tenant.id,
        "features": features,
    }


def is_tenant_feature_enabled(
    db: Session,
    feature_key: str,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
) -> bool:
    normalized_key = (feature_key or "").strip()
    if normalized_key not in RUNTIME_FEATURE_KEYS and normalized_key not in PRODUCT_RUNTIME_FEATURE_KEYS:
        return False

    runtime = get_tenant_feature_runtime(db, tenant_id=tenant_id, tenant=tenant)
    features = runtime.get("features")
    if not isinstance(features, dict):
        return False

    return bool(features.get(normalized_key, False))
