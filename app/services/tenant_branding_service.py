from sqlalchemy.orm import Session

from app.models.tenant import Tenant, TenantBranding
from app.schemas.tenant_branding_update import TenantBrandingUpdatePayload
from app.services.tenant_context import get_default_tenant, resolve_current_tenant


# Default da marca Aumigão: roxo da logo (PURPLE[600] do theme do app). Antes era
# um verde (#315f29) desalinhado da marca — corrigido em 2026-06-10.
DEFAULT_PRIMARY_COLOR = "#6429E8"
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


def get_tenant_branding_runtime(db: Session, tenant_id: str | None = None, tenant: Tenant | None = None) -> dict[str, str | bool]:
    tenant = tenant or _resolve_tenant(db, tenant_id)
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
        "splash_image_url": _clean_text(branding.splash_image_url if branding else None) or "",
        "primary_color": _clean_text(branding.primary_color if branding else None) or DEFAULT_PRIMARY_COLOR,
        "secondary_color": _clean_text(branding.secondary_color if branding else None) or DEFAULT_SECONDARY_COLOR,
        "accent_color": _clean_text(branding.accent_color if branding else None) or "",
        "powered_by_enabled": powered_by_enabled,
        "version": (branding.published_version if branding and branding.published_version else 1),
    }


def update_tenant_branding_runtime(
    db: Session,
    tenant: Tenant,
    payload: TenantBrandingUpdatePayload,
    actor=None,
) -> dict[str, str | bool]:
    branding = tenant.branding
    if not branding:
        branding = TenantBranding(tenant_id=tenant.id, display_name=payload.display_name)
        db.add(branding)

    branding.display_name = payload.display_name
    branding.app_name = payload.app_name
    branding.logo_url = payload.logo_url
    branding.icon_url = payload.icon_url
    branding.splash_image_url = payload.splash_image_url
    branding.primary_color = payload.primary_color
    branding.secondary_color = payload.secondary_color
    branding.accent_color = payload.accent_color
    branding.powered_by_enabled = payload.powered_by_enabled
    # Incrementa a versao publicada para o cliente invalidar cache (spec §9.4).
    branding.published_version = (branding.published_version or 0) + 1

    # Auditoria (spec §14.3): registra a publicacao do branding (espelha audit_log).
    try:
        from app.services.admin_operational_event_service import record_admin_operational_event

        record_admin_operational_event(
            db, event_type="published", entity_type="branding", entity_id=tenant.id,
            title="Branding publicado", actor=actor,
            metadata={"version": branding.published_version},
        )
    except Exception:
        pass

    db.commit()
    db.refresh(branding)
    db.refresh(tenant)
    return get_tenant_branding_runtime(db, tenant=tenant)
