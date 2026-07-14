from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.data_cache import data_cache
from app.models.tenant import Tenant
from app.services.tenant_branding_service import (
    DEFAULT_PRIMARY_COLOR,
    DEFAULT_SECONDARY_COLOR,
    get_tenant_branding_runtime,
)
from app.services.tenant_commercial_service import (
    BILLING_ENABLED,
    BILLING_STATUS,
    COMMERCIAL_PLAN_LABELS,
    NEXT_RECOMMENDED_PLAN,
    get_tenant_commercial_runtime,
)
from app.services.tenant_context import get_default_tenant, resolve_current_tenant
from app.services.tenant_feature_runtime_service import get_default_feature_runtime, get_tenant_feature_runtime
from app.services.tenant_plan_service import TENANT_PLAN_STARTER, get_plan_capabilities, get_tenant_capabilities
from app.services.tenant_unit_runtime_service import get_tenant_units_runtime


def _safe_rollback(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        pass


def _resolve_tenant(db: Session, tenant_id: str | None = None, request: Request | None = None) -> Tenant:
    if tenant_id and tenant_id != "current":
        tenant = db.get(Tenant, tenant_id)
        if tenant:
            return tenant

        tenant = db.query(Tenant).filter(Tenant.slug == tenant_id).first()
        if tenant:
            return tenant

        return get_default_tenant(db)

    return resolve_current_tenant(db, request)


def _fallback_branding(tenant: Tenant | None = None) -> dict[str, Any]:
    display_name = (tenant.name.strip() if tenant and tenant.name else "") or "Aumigao"
    return {
        "display_name": display_name,
        "app_name": display_name,
        "logo_url": "",
        "icon_url": "",
        "splash_image_url": "",
        "primary_color": DEFAULT_PRIMARY_COLOR,
        "secondary_color": DEFAULT_SECONDARY_COLOR,
        "accent_color": "",
        "powered_by_enabled": True,
    }


def _fallback_commercial() -> dict[str, Any]:
    next_plan = NEXT_RECOMMENDED_PLAN[TENANT_PLAN_STARTER]
    return {
        "plan": TENANT_PLAN_STARTER,
        "plan_label": COMMERCIAL_PLAN_LABELS[TENANT_PLAN_STARTER],
        "upgrade_available": next_plan is not None,
        "next_recommended_plan": next_plan,
        "billing_enabled": BILLING_ENABLED,
        "billing_status": BILLING_STATUS,
    }


def _safe_branding(db: Session, tenant: Tenant | None) -> dict[str, Any]:
    try:
        runtime = get_tenant_branding_runtime(db, tenant=tenant) if tenant else get_tenant_branding_runtime(db)
        return {
            "display_name": runtime.get("display_name") or "",
            "app_name": runtime.get("app_name") or "",
            "logo_url": runtime.get("logo_url") or "",
            "icon_url": runtime.get("icon_url") or "",
            "splash_image_url": runtime.get("splash_image_url") or "",
            "primary_color": runtime.get("primary_color") or DEFAULT_PRIMARY_COLOR,
            "secondary_color": runtime.get("secondary_color") or DEFAULT_SECONDARY_COLOR,
            # accent_color: o admin edita e o app usa no tema dinâmico do tenant —
            # sem esta linha o valor salvo nunca chega ao app (funil do app-config).
            "accent_color": runtime.get("accent_color") or "",
            "powered_by_enabled": bool(runtime.get("powered_by_enabled", True)),
        }
    except Exception:
        _safe_rollback(db)
        return _fallback_branding(tenant)


def _safe_features(db: Session, tenant: Tenant | None) -> dict[str, bool]:
    try:
        runtime = get_tenant_feature_runtime(db, tenant=tenant) if tenant else get_tenant_feature_runtime(db)
        features = runtime.get("features")
        if isinstance(features, dict):
            fallback = get_default_feature_runtime()
            return {feature_key: bool(features.get(feature_key, fallback[feature_key])) for feature_key in fallback}
    except Exception:
        _safe_rollback(db)
    return get_default_feature_runtime()


def _safe_units(db: Session, tenant: Tenant | None) -> list[dict[str, Any]]:
    try:
        runtime = get_tenant_units_runtime(db, tenant=tenant) if tenant else get_tenant_units_runtime(db)
        units = runtime.get("units")
        return units if isinstance(units, list) else []
    except Exception:
        _safe_rollback(db)
        return []


def _safe_commercial(db: Session, tenant: Tenant | None) -> dict[str, Any]:
    try:
        runtime = get_tenant_commercial_runtime(db, tenant=tenant) if tenant else get_tenant_commercial_runtime(db)
        fallback = _fallback_commercial()
        return {
            "plan": runtime.get("plan") or fallback["plan"],
            "plan_label": runtime.get("plan_label") or fallback["plan_label"],
            "upgrade_available": bool(runtime.get("upgrade_available", fallback["upgrade_available"])),
            "next_recommended_plan": runtime.get("next_recommended_plan"),
            "billing_enabled": bool(runtime.get("billing_enabled", fallback["billing_enabled"])),
            "billing_status": runtime.get("billing_status") or fallback["billing_status"],
        }
    except Exception:
        _safe_rollback(db)
        return _fallback_commercial()


def _safe_capabilities(db: Session, tenant: Tenant | None) -> dict[str, Any]:
    try:
        if tenant:
            return get_tenant_capabilities(tenant, db)
    except Exception:
        _safe_rollback(db)
    return get_plan_capabilities(TENANT_PLAN_STARTER)


def _safe_background_provider(db: Session, tenant: Any | None) -> str:
    """Retorna o background_check_provider do tenant (default 'manual').

    Expoe o provider ao app para que o cliente saiba qual fluxo de coleta
    de antecedentes exibir. Com a flag background_checks OFF (default), o
    app ignora este campo — mas e conveniente ja ter disponivel.
    """
    try:
        if tenant:
            from app.models.tenant import TenantSettings
            settings = db.query(TenantSettings).filter(
                TenantSettings.tenant_id == tenant.id
            ).first()
            if settings and settings.background_check_provider:
                return settings.background_check_provider
    except Exception:
        _safe_rollback(db)
    return "manual"


# TTL curto: staleness máxima de 60s para QUALQUER mudança de config que não
# passe pela invalidação explícita (features, units, plano). O write de
# branding invalida na hora (ver update_current_branding em tenant_branding.py).
APP_CONFIG_CACHE_TTL_SECONDS = 60


def app_config_cache_key(tenant_id: str) -> str:
    return f"tenant_app_config:{tenant_id}"


def invalidate_tenant_app_config_cache(tenant_id: str | None) -> None:
    """Derruba o app-config cacheado do tenant. Chamar após writes de config."""
    if tenant_id:
        data_cache.delete(app_config_cache_key(tenant_id))


def get_tenant_app_config(
    db: Session,
    tenant_id: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    try:
        tenant = _resolve_tenant(db, tenant_id=tenant_id, request=request)
    except Exception:
        _safe_rollback(db)
        tenant = None

    # Resposta é pública e idêntica para todos os callers do mesmo tenant —
    # segura de cachear por tenant.id. Sem tenant resolvido, não cacheia.
    cache_key = app_config_cache_key(tenant.id) if tenant else None
    if cache_key:
        cached = data_cache.get_json(cache_key)
        if cached is not None:
            return cached

    result = {
        "tenant_id": tenant.id if tenant else "",
        "branding": _safe_branding(db, tenant),
        "features": _safe_features(db, tenant),
        "units": _safe_units(db, tenant),
        "commercial": _safe_commercial(db, tenant),
        "capabilities": _safe_capabilities(db, tenant),
        "background_check_provider": _safe_background_provider(db, tenant),
    }
    if cache_key:
        data_cache.set_json(cache_key, result, APP_CONFIG_CACHE_TTL_SECONDS)
    return result
