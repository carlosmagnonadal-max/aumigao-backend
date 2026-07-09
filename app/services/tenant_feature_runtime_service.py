from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.services.tenant_context import get_default_tenant
from app.services.tenant_plan_service import (
    DEFAULT_ON_FEATURE_KEYS,
    FEATURE_CAPABILITY_KEYS,
    get_plan_capabilities,
    get_tenant_capabilities,
    tenant_feature_enabled,
    tenant_has_feature,
)


RUNTIME_FEATURE_KEYS = (
    "network_access",
    "dedicated_app",
    "custom_products",
    "custom_projects",
)

# Features de produto (flag direta por tenant, NAO gated por plano) expostas ao app
# para gatear exibicao.
# verified_walkers: default-OFF (legado).
# Demais keys novas: default-ON conforme DEFAULT_ON_FEATURE_KEYS.
PRODUCT_RUNTIME_FEATURE_KEYS = (
    "verified_walkers",
    "tips",
    "weekly_missions",
    "tutor_gamification",
    "protected_chat",
    "live_gps",
    "client_referrals",
    "walker_referrals",
    "reviews",
    "walker_boosts",
    "home_pickup",
    # Código de Coleta no pet-handover (mig 0105). Default-ON; o app do walker
    # consulta via useTenantFeature para decidir se abre o sheet de 4 dígitos.
    "pickup_code_required",
    "push_notifications",
    "transactional_emails",
    "support_tickets",
    # Background Check Fase 0 — gate de antecedentes do passeador. Default-OFF:
    # NAO esta em DEFAULT_ON_FEATURE_KEYS, entao parte desligada => ZERO regressao.
    "background_checks",
    # Modalidades de plano (gated por plano Business+ e por TenantFeature). Default-OFF
    # (NAO estao em DEFAULT_ON_FEATURE_KEYS). Precisam estar AQUI para o app-config
    # expor o valor real da flag ao app — senao o app nunca sabe que estao ligadas e
    # esconde os cards de Passeio Compartilhado / Pet Tour / Planos mensais.
    "recurring_plans",
    "shared_walks",
    "pet_tour",
    # Chaves que o app tutor consulta via useTenantFeature e que ficaram FORA do
    # payload quando nasceram (bug: o app caia no default fail-closed e escondia
    # Perfil Vivo, cupom no checkout e indique-e-ganhe mesmo com o toggle ON no
    # tenant). Default-OFF (nao estao em DEFAULT_ON_FEATURE_KEYS) => expor o valor
    # real da flag e zero regressao para quem nao ligou.
    "pet_live_profile",
    "coupons",
    "tutor_referrals",
)


def get_default_feature_runtime() -> dict[str, bool]:
    result = {}
    for feature_key in RUNTIME_FEATURE_KEYS:
        result[feature_key] = False
    for feature_key in PRODUCT_RUNTIME_FEATURE_KEYS:
        # Default-on keys partem de True; demais (ex.: verified_walkers) partem de False.
        result[feature_key] = feature_key in DEFAULT_ON_FEATURE_KEYS
    return result


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
        features[feature_key] = tenant_feature_enabled(resolved_tenant, db, feature_key)

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
        # Para keys nao registradas, usa DEFAULT_ON_FEATURE_KEYS como fallback
        return normalized_key in DEFAULT_ON_FEATURE_KEYS

    runtime = get_tenant_feature_runtime(db, tenant_id=tenant_id, tenant=tenant)
    features = runtime.get("features")
    if not isinstance(features, dict):
        return False

    return bool(features.get(normalized_key, normalized_key in DEFAULT_ON_FEATURE_KEYS))
