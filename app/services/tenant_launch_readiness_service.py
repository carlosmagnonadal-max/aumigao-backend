from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.services.tenant_app_config_service import get_tenant_app_config
from app.services.tenant_dedicated_app_readiness_service import get_tenant_dedicated_app_readiness


BLOCKING_CHECKS = (
    "app_name",
    "display_name",
    "primary_color",
    "secondary_color",
    "logo",
    "icon",
    "splash",
    "dedicated_app",
    "plan",
    "units",
)

LAUNCH_PLANS = {"business", "enterprise"}

DEFAULT_APP_CONFIG = {
    "tenant_id": "default",
    "branding": {
        "display_name": "Aumigao",
        "app_name": "Aumigao",
        "logo_url": "",
        "icon_url": "",
        "splash_image_url": "",
        "primary_color": "#6D28D9",
        "secondary_color": "#F97316",
        "powered_by_enabled": True,
    },
    "features": {
        "network_access": False,
        "dedicated_app": False,
        "custom_products": False,
        "custom_projects": False,
    },
    "units": [],
    "commercial": {
        "plan": "starter",
        "plan_label": "Starter",
        "upgrade_available": True,
        "next_recommended_plan": "business",
        "billing_enabled": False,
        "billing_status": "not_configured",
    },
    "capabilities": {},
}

DEFAULT_DEDICATED_READINESS = {
    "tenant_id": "default",
    "ready_for_dedicated_app": False,
    "dedicated_app_enabled": False,
    "missing": ["dedicated_app"],
    "asset_readiness": {
        "logo_missing": True,
        "icon_missing": True,
        "splash_missing": True,
    },
    "branding": DEFAULT_APP_CONFIG["branding"],
    "commercial": DEFAULT_APP_CONFIG["commercial"],
    "features": DEFAULT_APP_CONFIG["features"],
    "capabilities": {},
}


def _safe_rollback(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        pass


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_present(value: Any) -> bool:
    return bool(value.strip()) if isinstance(value, str) else False


def _safe_app_config(
    db: Session,
    tenant_id: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    try:
        config = get_tenant_app_config(db, tenant_id=tenant_id, request=request)
        return {**DEFAULT_APP_CONFIG, **_dict_value(config)}
    except Exception:
        _safe_rollback(db)
        return DEFAULT_APP_CONFIG


def _safe_dedicated_readiness(
    db: Session,
    tenant_id: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    try:
        readiness = get_tenant_dedicated_app_readiness(db, tenant_id=tenant_id, request=request)
        return {**DEFAULT_DEDICATED_READINESS, **_dict_value(readiness)}
    except Exception:
        _safe_rollback(db)
        return DEFAULT_DEDICATED_READINESS


def _launch_summary(ready: bool, score: int, blocking_items: list[str], warnings: list[str]) -> str:
    if ready:
        return f"Tenant pronto para lancar app dedicado white-label. Score {score}%."
    if blocking_items:
        return f"Tenant ainda nao esta pronto para lancamento. {len(blocking_items)} item(ns) bloqueante(s) pendente(s)."
    if warnings:
        return f"Tenant sem bloqueios criticos, mas com {len(warnings)} aviso(s). Score {score}%."
    return f"Tenant ainda nao esta pronto para lancamento. Score {score}%."


def get_tenant_launch_readiness(
    db: Session,
    tenant_id: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    app_config = _safe_app_config(db, tenant_id=tenant_id, request=request)
    dedicated_readiness = _safe_dedicated_readiness(db, tenant_id=tenant_id, request=request)

    branding = {**DEFAULT_APP_CONFIG["branding"], **_dict_value(app_config.get("branding"))}
    commercial = {**DEFAULT_APP_CONFIG["commercial"], **_dict_value(app_config.get("commercial"))}
    units = _list_value(app_config.get("units"))

    app_name = _string_present(branding.get("app_name"))
    display_name = _string_present(branding.get("display_name"))
    primary_color = _string_present(branding.get("primary_color"))
    secondary_color = _string_present(branding.get("secondary_color"))
    logo = _string_present(branding.get("logo_url"))
    icon = _string_present(branding.get("icon_url"))
    splash = _string_present(branding.get("splash_image_url"))
    dedicated_app = dedicated_readiness.get("dedicated_app_enabled") is True
    plan = str(commercial.get("plan") or "").strip().lower() in LAUNCH_PLANS
    billing = False
    active_units = any(isinstance(unit, dict) and unit.get("enabled") is True for unit in units)

    checks = {
        "branding": bool(app_name and display_name and primary_color and secondary_color),
        "app_name": app_name,
        "display_name": display_name,
        "primary_color": primary_color,
        "secondary_color": secondary_color,
        "logo": logo,
        "icon": icon,
        "splash": splash,
        "dedicated_app": dedicated_app,
        "plan": plan,
        "billing": billing,
        "units": active_units,
    }
    blocking_items = [check for check in BLOCKING_CHECKS if not checks[check]]
    warnings = ["billing_not_configured"] if not billing else []
    ready = not blocking_items
    score = int(round((sum(1 for passed in checks.values() if passed) / len(checks)) * 100))

    return {
        "tenant_id": app_config.get("tenant_id") or dedicated_readiness.get("tenant_id") or "",
        "ready": ready,
        "score": score,
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "blocking_items": blocking_items,
        "warnings": warnings,
        "summary": _launch_summary(ready, score, blocking_items, warnings),
    }
