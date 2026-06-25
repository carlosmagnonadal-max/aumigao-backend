from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.services.tenant_context import get_default_tenant
from app.services.tenant_feature_runtime_service import get_tenant_feature_runtime
from app.services.tenant_plan_service import (
    TENANT_PLAN_BUSINESS,
    TENANT_PLAN_ENTERPRISE,
    TENANT_PLAN_ENTERPRISE_V2,
    TENANT_PLAN_PRO_V2,
    TENANT_PLAN_STARTER,
    _PRICING_V2_ENABLED,
    _canonical_v2,
    get_plan_capabilities,
    get_tenant_capabilities,
)


COMMERCIAL_FEATURE_KEYS = (
    "network_access",
    "dedicated_app",
    "custom_products",
    "custom_projects",
)

# ── Catálogo v1 (legado — 3 planos) ─────────────────────────────────────────

COMMERCIAL_PLAN_LABELS = {
    TENANT_PLAN_STARTER: "Starter",
    TENANT_PLAN_BUSINESS: "Business",
    TENANT_PLAN_ENTERPRISE: "Enterprise",
}

COMMERCIAL_PLAN_DESCRIPTIONS = {
    TENANT_PLAN_STARTER: "Plano inicial para operacoes white-label.",
    TENANT_PLAN_BUSINESS: "Plano para operacoes em crescimento com recursos de marca dedicada.",
    TENANT_PLAN_ENTERPRISE: "Plano avancado para operacoes completas e projetos customizados.",
}

COMMERCIAL_PLAN_RECOMMENDED_FOR = {
    TENANT_PLAN_STARTER: ["validacao inicial", "operacao local"],
    TENANT_PLAN_BUSINESS: ["marca dedicada", "rede de walkers", "produtos customizados"],
    TENANT_PLAN_ENTERPRISE: ["multi-unidades", "projetos customizados", "operacao enterprise"],
}

COMMERCIAL_PLAN_FEATURES = {
    TENANT_PLAN_STARTER: {
        "network_access": False,
        "dedicated_app": False,
        "custom_products": False,
        "custom_projects": False,
    },
    TENANT_PLAN_BUSINESS: {
        "network_access": True,
        "dedicated_app": True,
        "custom_products": True,
        "custom_projects": False,
    },
    TENANT_PLAN_ENTERPRISE: {
        "network_access": True,
        "dedicated_app": True,
        "custom_products": True,
        "custom_projects": True,
    },
}

NEXT_RECOMMENDED_PLAN = {
    TENANT_PLAN_STARTER: TENANT_PLAN_BUSINESS,
    TENANT_PLAN_BUSINESS: TENANT_PLAN_ENTERPRISE,
    TENANT_PLAN_ENTERPRISE: None,
}

# ── Catálogo v2 (2 planos canônicos) ────────────────────────────────────────
# Controlado por PRICING_V2_ENABLED (default False → legado ativo).
#
# dedicated_app:
#   Pro      → add-on disponível (allowed=True), mas NÃO automático (required=False).
#   Enterprise → incluído (allowed=True, required=True).

COMMERCIAL_PLAN_LABELS_V2 = {
    TENANT_PLAN_PRO_V2: "Pro",
    TENANT_PLAN_ENTERPRISE_V2: "Enterprise",
}

COMMERCIAL_PLAN_DESCRIPTIONS_V2 = {
    TENANT_PLAN_PRO_V2: "Plano Pro para operacoes em crescimento com rede de walkers.",
    TENANT_PLAN_ENTERPRISE_V2: "Plano Enterprise com app dedicado e projetos customizados.",
}

COMMERCIAL_PLAN_RECOMMENDED_FOR_V2 = {
    TENANT_PLAN_PRO_V2: ["marca white-label", "rede de walkers", "ate 2 unidades"],
    TENANT_PLAN_ENTERPRISE_V2: ["app dedicado", "multi-unidades (ate 4)", "projetos customizados"],
}

COMMERCIAL_PLAN_FEATURES_V2 = {
    TENANT_PLAN_PRO_V2: {
        "network_access": True,
        "dedicated_app": False,   # add-on disponível mas NÃO incluído no plano base
        "custom_products": True,
        "custom_projects": False,
    },
    TENANT_PLAN_ENTERPRISE_V2: {
        "network_access": True,
        "dedicated_app": True,    # incluído no plano
        "custom_products": True,
        "custom_projects": True,
    },
}

NEXT_RECOMMENDED_PLAN_V2 = {
    TENANT_PLAN_PRO_V2: TENANT_PLAN_ENTERPRISE_V2,
    TENANT_PLAN_ENTERPRISE_V2: None,
}

BILLING_ENABLED = False
BILLING_STATUS = "not_configured"


def normalize_commercial_plan(plan: str | None) -> str:
    """Normaliza o plano para o catálogo ativo (v1 ou v2 via flag).

    Flag OFF: normaliza para chave v1 (starter/business/enterprise).
    Flag ON:  normaliza para chave v2 (pro/enterprise).
    Chaves desconhecidas → plano mínimo (starter em v1, pro em v2).
    """
    normalized = (plan or "").strip().lower()
    if _PRICING_V2_ENABLED:
        canon = _canonical_v2(normalized)
        return canon if canon in COMMERCIAL_PLAN_FEATURES_V2 else TENANT_PLAN_PRO_V2
    if normalized in COMMERCIAL_PLAN_FEATURES:
        return normalized
    return TENANT_PLAN_STARTER


def get_default_commercial_features() -> dict[str, bool]:
    if _PRICING_V2_ENABLED:
        return deepcopy(COMMERCIAL_PLAN_FEATURES_V2[TENANT_PLAN_PRO_V2])
    return deepcopy(COMMERCIAL_PLAN_FEATURES[TENANT_PLAN_STARTER])


def get_commercial_plan_features(plan: str | None) -> dict[str, bool]:
    if _PRICING_V2_ENABLED:
        canon = normalize_commercial_plan(plan)
        return deepcopy(COMMERCIAL_PLAN_FEATURES_V2.get(canon, COMMERCIAL_PLAN_FEATURES_V2[TENANT_PLAN_PRO_V2]))
    return deepcopy(COMMERCIAL_PLAN_FEATURES[normalize_commercial_plan(plan)])


def get_commercial_plans() -> dict[str, list[dict[str, Any]]]:
    """Retorna o catálogo de planos ativo.

    Flag OFF (default): catálogo legado (starter/business/enterprise).
    Flag ON:  catálogo v2 (pro/enterprise).
    """
    if _PRICING_V2_ENABLED:
        plans = []
        for plan in (TENANT_PLAN_PRO_V2, TENANT_PLAN_ENTERPRISE_V2):
            plans.append(
                {
                    "key": plan,
                    "label": COMMERCIAL_PLAN_LABELS_V2[plan],
                    "description": COMMERCIAL_PLAN_DESCRIPTIONS_V2[plan],
                    "capabilities": get_commercial_plan_features(plan),
                    "recommended_for": COMMERCIAL_PLAN_RECOMMENDED_FOR_V2[plan],
                }
            )
        return {"plans": plans}
    plans = []
    for plan in (TENANT_PLAN_STARTER, TENANT_PLAN_BUSINESS, TENANT_PLAN_ENTERPRISE):
        plans.append(
            {
                "key": plan,
                "label": COMMERCIAL_PLAN_LABELS[plan],
                "description": COMMERCIAL_PLAN_DESCRIPTIONS[plan],
                "capabilities": get_commercial_plan_features(plan),
                "recommended_for": COMMERCIAL_PLAN_RECOMMENDED_FOR[plan],
            }
        )
    return {"plans": plans}


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


def _safe_effective_capabilities(tenant: Tenant, db: Session) -> dict[str, Any]:
    try:
        return get_tenant_capabilities(tenant, db)
    except Exception:
        fallback = TENANT_PLAN_PRO_V2 if _PRICING_V2_ENABLED else TENANT_PLAN_STARTER
        return get_plan_capabilities(fallback)


def _safe_effective_features(db: Session, tenant: Tenant, plan: str) -> dict[str, bool]:
    try:
        runtime = get_tenant_feature_runtime(db, tenant=tenant)
        features = runtime.get("features")
        if isinstance(features, dict):
            return {feature_key: bool(features.get(feature_key, False)) for feature_key in COMMERCIAL_FEATURE_KEYS}
    except Exception:
        pass
    return get_commercial_plan_features(plan)


def _plan_label(plan: str) -> str:
    """Retorna o label do plano para o catálogo ativo (v1 ou v2)."""
    if _PRICING_V2_ENABLED:
        return COMMERCIAL_PLAN_LABELS_V2.get(plan, plan)
    return COMMERCIAL_PLAN_LABELS.get(plan, plan)


def _next_recommended(plan: str) -> str | None:
    """Retorna o próximo plano recomendado para o catálogo ativo."""
    if _PRICING_V2_ENABLED:
        return NEXT_RECOMMENDED_PLAN_V2.get(plan)
    return NEXT_RECOMMENDED_PLAN.get(plan)


def get_tenant_commercial_runtime(
    db: Session,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
) -> dict[str, Any]:
    """Retorna o estado comercial do tenant em runtime.

    Flag OFF (default): usa catálogo v1 (starter/business/enterprise) — zero-regressão.
    Flag ON:  usa catálogo v2 (pro/enterprise), mapeando chaves legadas automaticamente.
    """
    try:
        resolved_tenant = _resolve_tenant(db, tenant_id=tenant_id, tenant=tenant)
        plan = normalize_commercial_plan(resolved_tenant.plan)
        next_plan = _next_recommended(plan)

        return {
            "tenant_id": resolved_tenant.id,
            "plan": plan,
            "plan_label": _plan_label(plan),
            "capabilities": _safe_effective_capabilities(resolved_tenant, db),
            "features": _safe_effective_features(db, resolved_tenant, plan),
            "upgrade_available": next_plan is not None,
            "next_recommended_plan": next_plan,
            "billing_enabled": BILLING_ENABLED,
            "billing_status": BILLING_STATUS,
        }
    except Exception:
        if _PRICING_V2_ENABLED:
            plan = TENANT_PLAN_PRO_V2
        else:
            plan = TENANT_PLAN_STARTER
        next_plan = _next_recommended(plan)
        return {
            "tenant_id": "",
            "plan": plan,
            "plan_label": _plan_label(plan),
            "capabilities": get_plan_capabilities(plan),
            "features": get_default_commercial_features(),
            "upgrade_available": next_plan is not None,
            "next_recommended_plan": next_plan,
            "billing_enabled": BILLING_ENABLED,
            "billing_status": BILLING_STATUS,
        }
