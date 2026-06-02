from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.pet import Pet
from app.models.tenant import Tenant, TenantBranding, TenantFeature, TenantSettings, TenantUnit
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walk import Walk


DEFAULT_TENANT_SLUG = "aumigao"
DEFAULT_FEATURES = [
    "dedicated_app",
    "network_access",
    "multi_unit",
    "advanced_reports",
    "custom_products",
    "custom_projects",
    "powered_by_removable",
]


def ensure_default_tenant(db: Session) -> Tenant:
    tenant = db.query(Tenant).filter(Tenant.slug == DEFAULT_TENANT_SLUG).first()
    if not tenant:
        tenant = Tenant(
            name="Aumigão",
            slug=DEFAULT_TENANT_SLUG,
            status="active",
            plan="enterprise",
        )
        db.add(tenant)
        db.flush()
    else:
        tenant.name = tenant.name or "Aumigão"
        tenant.status = tenant.status or "active"
        tenant.plan = tenant.plan or "enterprise"

    branding = db.query(TenantBranding).filter(TenantBranding.tenant_id == tenant.id).first()
    if not branding:
        db.add(
            TenantBranding(
                tenant_id=tenant.id,
                display_name="Aumigão",
                app_name="Aumigão",
                powered_by_enabled=True,
            )
        )

    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant.id).first()
    if not settings:
        db.add(TenantSettings(tenant_id=tenant.id, timezone="America/Bahia"))

    existing_features = {
        item.feature_key
        for item in db.query(TenantFeature).filter(TenantFeature.tenant_id == tenant.id).all()
    }
    for feature_key in DEFAULT_FEATURES:
        if feature_key not in existing_features:
            db.add(TenantFeature(tenant_id=tenant.id, feature_key=feature_key, enabled=False))

    existing_unit = db.query(TenantUnit).filter(TenantUnit.tenant_id == tenant.id).first()
    if not existing_unit:
        db.add(
            TenantUnit(
                tenant_id=tenant.id,
                name="Operação Principal",
                status="active",
                city="Salvador",
                state="BA",
            )
        )

    db.commit()
    db.refresh(tenant)
    return tenant


def ensure_default_tenant_links(db: Session) -> Tenant:
    tenant = ensure_default_tenant(db)
    updated = 0

    for model in (User, TutorProfile, Pet, Walk, Notification):
        updated += (
            db.query(model)
            .filter(model.tenant_id.is_(None))
            .update({model.tenant_id: tenant.id}, synchronize_session=False)
        )

    if updated:
        db.commit()

    return tenant


def default_tenant_id(db: Session) -> str:
    tenant = db.query(Tenant).filter(Tenant.slug == DEFAULT_TENANT_SLUG).first()
    if tenant:
        return tenant.id
    return ensure_default_tenant(db).id
