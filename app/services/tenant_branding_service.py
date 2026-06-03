from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.services.tenant_context import get_default_tenant, resolve_current_tenant


DEFAULT_PRIMARY_COLOR = "#315f29"
DEFAULT_SECONDARY_COLOR = "#101811"


def _clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolve_tenant(db: Session, tenant_id: str | None = None) -> Tenant:
    if not tenant_id or tenant_id == "current":
        return resolve_current_tenant(db)

    tenant = db.get(Tenant, tenant_id)
    if tenant:
        return tenant

    tenant = db.query(Tenant).filter(Tenant.slug == tenant_id).first()
    if tenant:
        return tenant

    return get_default_tenant(db)


def get_tenant_branding_runtime(db: Session, tenant_id: str | None = None) -> dict[str, str | bool]:
    tenant = _resolve_tenant(db, tenant_id)
    branding = tenant.branding

    display_name = _clean_text(branding.display_name if branding else None) or _clean_text(tenant.name) or "Aumigao"
    app_name = _clean_text(branding.app_name if branding else None) or display_name

    powered_by_enabled = branding.powered_by_enabled if branding and branding.powered_by_enabled is not None else True

    return {
        "tenant_id": tenant.id,
        "display_name": display_name,
        "app_name": app_name,
        "logo_url": _clean_text(branding.logo_url if branding else None) or "",
        "icon_url": _clean_text(branding.icon_url if branding else None) or "",
        "primary_color": _clean_text(branding.primary_color if branding else None) or DEFAULT_PRIMARY_COLOR,
        "secondary_color": _clean_text(branding.secondary_color if branding else None) or DEFAULT_SECONDARY_COLOR,
        "powered_by_enabled": powered_by_enabled,
    }
