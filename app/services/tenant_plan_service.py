import os
from copy import deepcopy
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantFeature, TenantUnit


# Funcionalidades que estao SEMPRE LIGADAS em producao (default-on).
# Uma linha na tabela TenantFeature permite DESLIGA-LAS individualmente.
# Chaves ausentes da tabela devolvem True para as keys aqui e False para as demais.
DEFAULT_ON_FEATURE_KEYS: frozenset[str] = frozenset({
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
    "push_notifications",
    "transactional_emails",
    "support_tickets",
})


def tenant_feature_enabled(tenant: Tenant, db: Session, key: str) -> bool:
    """Retorna se a feature esta habilitada para o tenant.

    Semantica:
    - Linha na TenantFeature presente → usa o campo `enabled`.
    - Linha ausente → True se key em DEFAULT_ON_FEATURE_KEYS, False caso contrario.

    Para features comerciais (plano-gated) use tenant_has_feature/enforce_tenant_feature_allowed.
    """
    row: TenantFeature | None = (
        db.query(TenantFeature)
        .filter(TenantFeature.tenant_id == tenant.id, TenantFeature.feature_key == key)
        .first()
    )
    if row is not None:
        return bool(row.enabled)
    return key in DEFAULT_ON_FEATURE_KEYS


TENANT_PLAN_STARTER = "starter"
TENANT_PLAN_BUSINESS = "business"
TENANT_PLAN_ENTERPRISE = "enterprise"

# ── Pricing v1 (legado — 3 planos) ──────────────────────────────────────────
TENANT_PLAN_CAPABILITIES: dict[str, dict[str, Any]] = {
    TENANT_PLAN_STARTER: {
        "max_units": 1,
        "max_units_with_addon": 1,
        "dedicated_app_allowed": False,
        "dedicated_app_required": False,
        "powered_by_required": True,
        "network_access_available": False,
        "custom_products_allowed": False,
        "custom_projects_allowed": False,
        "onboarding_mode": "self_service",
    },
    TENANT_PLAN_BUSINESS: {
        "max_units": 2,
        "max_units_with_addon": 3,
        "dedicated_app_allowed": True,
        "dedicated_app_required": False,
        "powered_by_required": False,
        "network_access_available": True,
        "custom_products_allowed": True,
        "custom_projects_allowed": False,
        "onboarding_mode": "assisted",
    },
    TENANT_PLAN_ENTERPRISE: {
        "max_units": None,
        "max_units_with_addon": None,
        "dedicated_app_allowed": True,
        "dedicated_app_required": True,
        "powered_by_required": False,
        "network_access_available": True,
        "custom_products_allowed": True,
        "custom_projects_allowed": True,
        "onboarding_mode": "consultative",
    },
}

# ── Pricing v2 (2 planos canônicos: Pro / Enterprise) ───────────────────────
# Controlado por PRICING_V2_ENABLED (default False → legado ativo, zero-regressão).
#
# Decisão Carlos 2026-06-23:
#   Pro (starter/business → pro):
#     max_units=2; dedicated_app=ADD-ON (não incluído por padrão);
#     network_access=True; custom_products=True.
#   Enterprise:
#     max_units=4; dedicated_app=INCLUÍDO (dedicated_app_required=True);
#     network_access=True; custom_products=True; custom_projects=True.
#
# dedicated_app é ADD-ON em Pro: dedicated_app_allowed=True (pode contratar),
# dedicated_app_required=False (NÃO vem por padrão no plano).
# Em Enterprise: dedicated_app_required=True (já incluído).

TENANT_PLAN_PRO_V2 = "pro"
TENANT_PLAN_ENTERPRISE_V2 = "enterprise"

# Mapeamento legado → canônico v2 (para resolução de capabilities).
_LEGACY_TO_V2: dict[str, str] = {
    "starter": TENANT_PLAN_PRO_V2,
    "business": TENANT_PLAN_PRO_V2,
    "enterprise": TENANT_PLAN_ENTERPRISE_V2,
    "pro": TENANT_PLAN_PRO_V2,
}

TENANT_PLAN_CAPABILITIES_V2: dict[str, dict[str, Any]] = {
    TENANT_PLAN_PRO_V2: {
        "max_units": 2,
        "max_units_with_addon": 2,          # Pro sem add-on de unidades extra
        "dedicated_app_allowed": True,       # Pode contratar como add-on
        "dedicated_app_required": False,     # NÃO incluído automaticamente no plano
        "powered_by_required": False,
        "network_access_available": True,
        "custom_products_allowed": True,
        "custom_projects_allowed": False,
        "onboarding_mode": "assisted",
    },
    TENANT_PLAN_ENTERPRISE_V2: {
        "max_units": 4,
        "max_units_with_addon": 4,
        "dedicated_app_allowed": True,       # Incluído
        "dedicated_app_required": True,      # App dedicado faz parte do plano
        "powered_by_required": False,
        "network_access_available": True,
        "custom_products_allowed": True,
        "custom_projects_allowed": True,
        "onboarding_mode": "consultative",
    },
}

# Módulos de produto gated por plano v2 (Pro = starter/business; Enterprise = enterprise).
PLAN_GATED_PRODUCT_FEATURES_V2: dict[str, set[str]] = {
    "recurring_plans": {TENANT_PLAN_PRO_V2, TENANT_PLAN_ENTERPRISE_V2},
    "shared_walks": {TENANT_PLAN_PRO_V2, TENANT_PLAN_ENTERPRISE_V2},
    "pet_tour": {TENANT_PLAN_PRO_V2, TENANT_PLAN_ENTERPRISE_V2},
}

_PRICING_V2_ENABLED: bool = os.getenv("PRICING_V2_ENABLED", "false").lower() in {"1", "true", "yes"}


def _canonical_v2(plan: str | None) -> str:
    """Mapeia chave legada → canônica v2 (pro/enterprise). Uso interno."""
    normalized = (plan or "").strip().lower()
    return _LEGACY_TO_V2.get(normalized, TENANT_PLAN_PRO_V2)


FEATURE_CAPABILITY_KEYS = {
    "dedicated_app": "dedicated_app_allowed",
    "network_access": "network_access_available",
    "custom_products": "custom_products_allowed",
    "custom_projects": "custom_projects_allowed",
    "powered_by_required": "powered_by_required",
}

ENFORCED_COMMERCIAL_FEATURES = {
    "dedicated_app",
    "network_access",
    "custom_products",
    "custom_projects",
}

# Módulos de PRODUTO que só ficam disponíveis a partir de certo plano (legado v1).
# Ausência da chave aqui = disponível em todos os planos (ex.: coupons).
PLAN_GATED_PRODUCT_FEATURES: dict[str, set[str]] = {
    "recurring_plans": {TENANT_PLAN_BUSINESS, TENANT_PLAN_ENTERPRISE},
    "shared_walks": {TENANT_PLAN_BUSINESS, TENANT_PLAN_ENTERPRISE},
    "pet_tour": {TENANT_PLAN_BUSINESS, TENANT_PLAN_ENTERPRISE},
}


def get_plan_capabilities(plan: str) -> dict[str, Any]:
    """Retorna as capabilities do plano, respeitando PRICING_V2_ENABLED.

    Flag OFF (default): usa TENANT_PLAN_CAPABILITIES (legado 3 planos) — zero-regressão.
    Flag ON:  usa TENANT_PLAN_CAPABILITIES_V2 (2 planos canônicos).
              Chaves legadas (starter/business/enterprise) são mapeadas automaticamente.
              Chaves canônicas v2 (pro/enterprise) passam direto.
    """
    if _PRICING_V2_ENABLED:
        canon = _canonical_v2(plan)
        return deepcopy(TENANT_PLAN_CAPABILITIES_V2.get(canon, TENANT_PLAN_CAPABILITIES_V2[TENANT_PLAN_PRO_V2]))
    return deepcopy(TENANT_PLAN_CAPABILITIES.get(plan or "", TENANT_PLAN_CAPABILITIES[TENANT_PLAN_STARTER]))


def _coerce_limit(value: str | None):
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"", "none", "null", "unlimited"}:
        return None
    try:
        return int(normalized)
    except ValueError:
        return value


def _tenant_features(tenant: Tenant, db: Session) -> list[TenantFeature]:
    return db.query(TenantFeature).filter(TenantFeature.tenant_id == tenant.id).all()


def get_tenant_capabilities(tenant: Tenant, db: Session) -> dict[str, Any]:
    capabilities = get_plan_capabilities(tenant.plan)

    for feature in _tenant_features(tenant, db):
        feature_key = (feature.feature_key or "").strip()
        if feature.limit_value is not None:
            capabilities[feature_key] = _coerce_limit(feature.limit_value)
        if feature_key in FEATURE_CAPABILITY_KEYS:
            capabilities[FEATURE_CAPABILITY_KEYS[feature_key]] = bool(feature.enabled)
        elif feature.enabled and feature.limit_value is None:
            capabilities[feature_key] = True

    return capabilities


def tenant_has_feature(tenant: Tenant, db: Session, feature_key: str) -> bool:
    capabilities = get_tenant_capabilities(tenant, db)
    capability_key = FEATURE_CAPABILITY_KEYS.get(feature_key, feature_key)
    return bool(capabilities.get(capability_key))


def get_tenant_limit(tenant: Tenant, db: Session, limit_key: str):
    capabilities = get_tenant_capabilities(tenant, db)
    return capabilities.get(limit_key)


def can_add_tenant_unit(tenant: Tenant, db: Session) -> bool:
    limit = get_tenant_limit(tenant, db, "max_units_with_addon")
    if limit is None:
        return True
    if not isinstance(limit, int):
        return True
    current_units = db.query(TenantUnit).filter(TenantUnit.tenant_id == tenant.id).count()
    return current_units < limit


def enforce_can_add_tenant_unit(tenant: Tenant, db: Session) -> None:
    if not can_add_tenant_unit(tenant, db):
        raise HTTPException(status_code=403, detail="Limite de unidades atingido para o plano atual.")


def enforce_tenant_feature_allowed(tenant: Tenant, db: Session, feature_key: str) -> None:
    normalized_key = (feature_key or "").strip()
    if normalized_key not in ENFORCED_COMMERCIAL_FEATURES:
        return

    base_capabilities = get_plan_capabilities(tenant.plan)
    capability_key = FEATURE_CAPABILITY_KEYS.get(normalized_key, normalized_key)
    if not base_capabilities.get(capability_key):
        raise HTTPException(status_code=403, detail=f"Feature {normalized_key} indisponível para o plano atual.")


def tenant_tem_rede(tenant: Tenant, db: Session) -> bool:  # noqa: ARG001
    """Retorna se o tenant tem acesso à Rede Aumigão de passeadores.

    Hierarquia de decisão (Fase 1 PRD, decisão 5):
    1. override manual (super_admin): se network_access_override não for None → usa o valor.
    2. Regra de plano + addon:
       - enterprise → sempre True.
       - business   → True SOMENTE se network_access_addon=True.
         NOTE: business agora exige network_access_addon (decisão 5 PRD);
         migrar tenants business existentes setando network_access_addon=true
         antes de ligar a flag MULTI_TENANT_WALKER em produção.
       - starter/outros → False.

    O parâmetro db é mantido para compatibilidade futura (ex.: lookup de feature rows).
    """
    override = getattr(tenant, "network_access_override", None)
    if override is not None:
        return bool(override)

    plan = (tenant.plan or "").strip().lower()
    if plan == TENANT_PLAN_ENTERPRISE:
        return True
    # Pricing v2: plano canônico Pro inclui acesso à Rede (capability
    # network_access_available=True). Coerente com TENANT_PLAN_CAPABILITIES_V2.
    if plan == TENANT_PLAN_PRO_V2:
        return True
    if plan == TENANT_PLAN_BUSINESS:
        return bool(getattr(tenant, "network_access_addon", False))
    # starter e demais planos → sem rede
    return False


def enforce_network_access_allowed(tenant: Tenant, db: Session) -> None:
    if not tenant_tem_rede(tenant, db):
        raise HTTPException(status_code=403, detail="Acesso à Rede Aumigão indisponível para o plano atual.")


def enforce_tenant_product_feature(tenant: Tenant, db: Session, feature_key: str, label: str) -> None:
    """Gate genérico de feature de produto (não-comercial) habilitada por tenant.

    Diferente das features comerciais (gated por plano), features de produto da
    Onda 1+ (ex.: recurring_plans) são liberadas ligando a TenantFeature do tenant.
    """
    if not tenant_has_feature(tenant, db, feature_key):
        raise HTTPException(status_code=403, detail=f"{label} não está habilitado para este tenant.")


def plan_allows_product_feature(tenant: Tenant, feature_key: str) -> bool:
    """Se o PLANO do tenant permite o módulo de produto plano-gated.

    Flag OFF (default): usa PLAN_GATED_PRODUCT_FEATURES (legado v1 — 3 planos).
    Flag ON:  usa PLAN_GATED_PRODUCT_FEATURES_V2 com o plano canônico v2.
              Chaves legadas (starter/business) mapeiam para pro, que está na lista v2.
              Zero-regressão: módulos liberados em business/enterprise continuam
              liberados ao mapear para pro/enterprise v2.

    Módulos fora das listas de gating (ex.: coupons) ficam liberados em todos os planos.
    """
    key = (feature_key or "").strip()
    if _PRICING_V2_ENABLED:
        allowed_plans = PLAN_GATED_PRODUCT_FEATURES_V2.get(key)
        if allowed_plans is None:
            return True
        return _canonical_v2(tenant.plan) in allowed_plans
    allowed_plans = PLAN_GATED_PRODUCT_FEATURES.get(key)
    if allowed_plans is None:
        return True
    return (tenant.plan or "").strip().lower() in allowed_plans


def enforce_plan_allows_product_feature(
    tenant: Tenant, feature_key: str, label: str | None = None
) -> None:
    """Trava por PLANO de um módulo de produto (ex.: recorrência/Pet Tour = Business+).

    Independe da flag do tenant: bloqueia mesmo que a TenantFeature esteja ligada
    (protege contra flag-legado num plano que não deveria ter o módulo).
    """
    if not plan_allows_product_feature(tenant, feature_key):
        name = label or (feature_key or "").strip()
        raise HTTPException(
            status_code=403,
            detail=f"{name} está disponível a partir do plano Business.",
        )
