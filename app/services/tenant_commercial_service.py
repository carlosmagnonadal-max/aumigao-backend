from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.services.tenant_context import get_default_tenant
from app.services.tenant_feature_runtime_service import get_tenant_feature_runtime
from app.services.tenant_plan_service import (
    TENANT_PLAN_BUSINESS,
    TENANT_PLAN_ENTERPRISE,
    TENANT_PLAN_STARTER,
    get_plan_capabilities,
    get_tenant_capabilities,
)


COMMERCIAL_FEATURE_KEYS = (
    "network_access",
    "dedicated_app",
    "custom_products",
    "custom_projects",
)

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

BILLING_ENABLED = False
BILLING_STATUS = "not_configured"


def normalize_commercial_plan(plan: str | None) -> str:
    normalized = (plan or "").strip().lower()
    if normalized in COMMERCIAL_PLAN_FEATURES:
        return normalized
    return TENANT_PLAN_STARTER


def get_default_commercial_features() -> dict[str, bool]:
    return deepcopy(COMMERCIAL_PLAN_FEATURES[TENANT_PLAN_STARTER])


def get_commercial_plan_features(plan: str | None) -> dict[str, bool]:
    return deepcopy(COMMERCIAL_PLAN_FEATURES[normalize_commercial_plan(plan)])


def get_commercial_plans() -> dict[str, list[dict[str, Any]]]:
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
        return get_plan_capabilities(TENANT_PLAN_STARTER)


def _safe_effective_features(db: Session, tenant: Tenant, plan: str) -> dict[str, bool]:
    try:
        runtime = get_tenant_feature_runtime(db, tenant=tenant)
        features = runtime.get("features")
        if isinstance(features, dict):
            return {feature_key: bool(features.get(feature_key, False)) for feature_key in COMMERCIAL_FEATURE_KEYS}
    except Exception:
        pass
    return get_commercial_plan_features(plan)


def get_tenant_commercial_runtime(
    db: Session,
    tenant_id: str | None = None,
    tenant: Tenant | None = None,
) -> dict[str, Any]:
    try:
        resolved_tenant = _resolve_tenant(db, tenant_id=tenant_id, tenant=tenant)
        plan = normalize_commercial_plan(resolved_tenant.plan)
        next_plan = NEXT_RECOMMENDED_PLAN[plan]

        return {
            "tenant_id": resolved_tenant.id,
            "plan": plan,
            "plan_label": COMMERCIAL_PLAN_LABELS[plan],
            "capabilities": _safe_effective_capabilities(resolved_tenant, db),
            "features": _safe_effective_features(db, resolved_tenant, plan),
            "upgrade_available": next_plan is not None,
            "next_recommended_plan": next_plan,
            "billing_enabled": BILLING_ENABLED,
            "billing_status": BILLING_STATUS,
        }
    except Exception:
        plan = TENANT_PLAN_STARTER
        next_plan = NEXT_RECOMMENDED_PLAN[plan]
        return {
            "tenant_id": "",
            "plan": plan,
            "plan_label": COMMERCIAL_PLAN_LABELS[plan],
            "capabilities": get_plan_capabilities(plan),
            "features": get_default_commercial_features(),
            "upgrade_available": next_plan is not None,
            "next_recommended_plan": next_plan,
            "billing_enabled": BILLING_ENABLED,
            "billing_status": BILLING_STATUS,
        }
