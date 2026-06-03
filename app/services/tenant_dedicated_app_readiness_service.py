from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.services.tenant_app_config_service import get_tenant_app_config


DEDICATED_APP_CAPABILITY_KEYS = (
    "dedicated_app",
    "dedicated_app_enabled",
    "dedicatedApp",
    "dedicatedAppEnabled",
)


DEFAULT_BRANDING = {
    "display_name": "Aumigao",
    "app_name": "Aumigao",
    "logo_url": "",
    "icon_url": "",
    "splash_image_url": "",
    "primary_color": "#6D28D9",
    "secondary_color": "#F97316",
    "powered_by_enabled": True,
}

DEFAULT_COMMERCIAL = {
    "plan": "starter",
    "plan_label": "Starter",
    "upgrade_available": True,
    "next_recommended_plan": "business",
    "billing_enabled": False,
    "billing_status": "not_configured",
}

DEFAULT_FEATURES = {
    "network_access": False,
    "dedicated_app": False,
    "custom_products": False,
    "custom_projects": False,
}


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _capability_enabled(capabilities: dict[str, Any]) -> bool:
    return any(capabilities.get(key) is True for key in DEDICATED_APP_CAPABILITY_KEYS)


def _safe_rollback(db: Session) -> None:
    try:
        db.rollback()
    except Exception:
        pass


def _fallback_app_config() -> dict[str, Any]:
    return {
        "tenant_id": "default",
        "branding": DEFAULT_BRANDING,
        "features": DEFAULT_FEATURES,
        "commercial": DEFAULT_COMMERCIAL,
        "capabilities": {},
    }


def get_tenant_dedicated_app_readiness(
    db: Session,
    tenant_id: str | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    try:
        app_config = get_tenant_app_config(db, tenant_id=tenant_id, request=request)
    except Exception:
        _safe_rollback(db)
        app_config = _fallback_app_config()

    branding = {**DEFAULT_BRANDING, **_dict_value(app_config.get("branding"))}
    features = {**DEFAULT_FEATURES, **_dict_value(app_config.get("features"))}
    commercial = {**DEFAULT_COMMERCIAL, **_dict_value(app_config.get("commercial"))}
    capabilities = _dict_value(app_config.get("capabilities"))

    app_name = _string_value(branding.get("app_name"))
    display_name = _string_value(branding.get("display_name"))
    logo_url = _string_value(branding.get("logo_url"))
    icon_url = _string_value(branding.get("icon_url"))
    splash_image_url = _string_value(branding.get("splash_image_url"))
    primary_color = _string_value(branding.get("primary_color"))
    secondary_color = _string_value(branding.get("secondary_color"))

    dedicated_app_enabled = features.get("dedicated_app") is True or _capability_enabled(capabilities)
    asset_readiness = {
        "logo_missing": not bool(logo_url),
        "icon_missing": not bool(icon_url),
        "splash_missing": not bool(splash_image_url),
    }

    missing = [
        "dedicated_app" if not dedicated_app_enabled else "",
        "app_name" if not app_name else "",
        "display_name" if not display_name else "",
        "primary_color" if not primary_color else "",
        "secondary_color" if not secondary_color else "",
        "logo_url" if asset_readiness["logo_missing"] else "",
        "icon_url" if asset_readiness["icon_missing"] else "",
        "splash_image_url" if asset_readiness["splash_missing"] else "",
    ]

    return {
        "tenant_id": app_config.get("tenant_id") or "",
        "ready_for_dedicated_app": bool(
            dedicated_app_enabled and app_name and display_name and primary_color and secondary_color
        ),
        "dedicated_app_enabled": dedicated_app_enabled,
        "missing": [item for item in missing if item],
        "asset_readiness": asset_readiness,
        "branding": branding,
        "commercial": commercial,
        "features": features,
        "capabilities": capabilities,
    }
