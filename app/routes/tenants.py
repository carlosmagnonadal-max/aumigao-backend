from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import require_admin
from app.models.tenant import Tenant, TenantBranding, TenantFeature, TenantSettings, TenantUnit
from app.models.tenant_onboarding import TenantOnboarding
from app.schemas.tenant import (
    TENANT_PLANS,
    TENANT_STATUSES,
    TENANT_UNIT_STATUSES,
    TenantBrandingResponse,
    TenantBrandingUpdate,
    TenantCreate,
    TenantDetailResponse,
    TenantFeatureResponse,
    TenantFeatureUpdate,
    TenantResponse,
    TenantSettingsResponse,
    TenantSettingsUpdate,
    TenantUnitCreate,
    TenantUnitResponse,
    TenantUpdate,
)
from app.schemas.tenant_onboarding import (
    TENANT_ONBOARDING_STATUSES,
    TenantOnboardingResponse,
    TenantOnboardingUpdate,
)
from app.schemas.tenant_plan import TenantCapabilitiesResponse
from app.services.tenant_plan_service import get_tenant_capabilities

router = APIRouter(prefix="/admin/tenants", tags=["admin-tenants"], dependencies=[Depends(require_admin)])
api_router = APIRouter(prefix="/api/admin/tenants", tags=["admin-tenants"], dependencies=[Depends(require_admin)])


def _normalize_slug(value: str) -> str:
    slug = (value or "").strip().lower()
    if not slug:
        raise HTTPException(status_code=400, detail="slug obrigatório.")
    return slug


def _tenant_or_404(tenant_id: str, db: Session) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant não encontrado.")
    return tenant


def _ensure_status(value: str | None, allowed: set[str], field_name: str) -> None:
    if value is not None and value not in allowed:
        raise HTTPException(status_code=400, detail=f"{field_name} inválido.")


def _ensure_plan(value: str | None) -> None:
    if value is not None and value not in TENANT_PLANS:
        raise HTTPException(status_code=400, detail="plan inválido.")


def _default_branding(tenant: Tenant) -> TenantBranding:
    return TenantBranding(
        tenant_id=tenant.id,
        display_name=tenant.name,
        app_name=tenant.name,
        powered_by_enabled=True,
    )


def _default_settings(tenant: Tenant) -> TenantSettings:
    return TenantSettings(tenant_id=tenant.id, timezone="America/Bahia")


def _default_onboarding(tenant: Tenant) -> TenantOnboarding:
    return TenantOnboarding(tenant_id=tenant.id, onboarding_status="created")


def _ensure_tenant_onboarding(tenant: Tenant, db: Session) -> TenantOnboarding:
    onboarding = tenant.onboarding
    if onboarding:
        return onboarding
    onboarding = _default_onboarding(tenant)
    db.add(onboarding)
    db.commit()
    db.refresh(onboarding)
    return onboarding


@router.get("", response_model=list[TenantResponse])
@api_router.get("", response_model=list[TenantResponse])
def list_tenants(db: Session = Depends(get_db)):
    return db.query(Tenant).order_by(Tenant.created_at.desc()).all()


@router.post("", response_model=TenantDetailResponse)
@api_router.post("", response_model=TenantDetailResponse)
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)):
    slug = _normalize_slug(payload.slug)
    _ensure_status(payload.status, TENANT_STATUSES, "status")
    _ensure_plan(payload.plan)
    existing = db.query(Tenant).filter(Tenant.slug == slug).first()
    if existing:
        raise HTTPException(status_code=409, detail="Tenant com este slug já existe.")

    tenant = Tenant(
        name=payload.name.strip(),
        slug=slug,
        status=payload.status,
        plan=payload.plan,
        legal_name=payload.legal_name,
        document_number=payload.document_number,
        contact_email=payload.contact_email,
        contact_phone=payload.contact_phone,
    )
    db.add(tenant)
    db.flush()
    db.add(_default_branding(tenant))
    db.add(_default_settings(tenant))
    db.add(_default_onboarding(tenant))
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/{tenant_id}", response_model=TenantDetailResponse)
@api_router.get("/{tenant_id}", response_model=TenantDetailResponse)
def get_tenant(tenant_id: str, db: Session = Depends(get_db)):
    return _tenant_or_404(tenant_id, db)


@router.patch("/{tenant_id}", response_model=TenantResponse)
@api_router.patch("/{tenant_id}", response_model=TenantResponse)
def update_tenant(tenant_id: str, payload: TenantUpdate, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    values = payload.model_dump(exclude_unset=True)
    _ensure_status(values.get("status"), TENANT_STATUSES, "status")
    _ensure_plan(values.get("plan"))
    for field, value in values.items():
        setattr(tenant, field, value.strip() if isinstance(value, str) else value)
    tenant.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/{tenant_id}/branding", response_model=TenantBrandingResponse)
@api_router.get("/{tenant_id}/branding", response_model=TenantBrandingResponse)
def get_tenant_branding(tenant_id: str, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    if not tenant.branding:
        tenant.branding = _default_branding(tenant)
        db.commit()
        db.refresh(tenant.branding)
    return tenant.branding


@router.patch("/{tenant_id}/branding", response_model=TenantBrandingResponse)
@api_router.patch("/{tenant_id}/branding", response_model=TenantBrandingResponse)
def update_tenant_branding(tenant_id: str, payload: TenantBrandingUpdate, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    branding = tenant.branding or _default_branding(tenant)
    db.add(branding)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(branding, field, value.strip() if isinstance(value, str) else value)
    branding.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(branding)
    return branding


@router.get("/{tenant_id}/settings", response_model=TenantSettingsResponse)
@api_router.get("/{tenant_id}/settings", response_model=TenantSettingsResponse)
def get_tenant_settings(tenant_id: str, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    if not tenant.settings:
        tenant.settings = _default_settings(tenant)
        db.commit()
        db.refresh(tenant.settings)
    return tenant.settings


@router.patch("/{tenant_id}/settings", response_model=TenantSettingsResponse)
@api_router.patch("/{tenant_id}/settings", response_model=TenantSettingsResponse)
def update_tenant_settings(tenant_id: str, payload: TenantSettingsUpdate, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    settings = tenant.settings or _default_settings(tenant)
    db.add(settings)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings, field, value.strip() if isinstance(value, str) else value)
    settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(settings)
    return settings


@router.get("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
@api_router.get("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
def get_tenant_onboarding(tenant_id: str, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    return _ensure_tenant_onboarding(tenant, db)


@router.get("/{tenant_id}/capabilities", response_model=TenantCapabilitiesResponse)
@api_router.get("/{tenant_id}/capabilities", response_model=TenantCapabilitiesResponse)
def get_tenant_capabilities_endpoint(tenant_id: str, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    return {
        "tenant_id": tenant.id,
        "plan": tenant.plan,
        "capabilities": get_tenant_capabilities(tenant, db),
    }


@router.patch("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
@api_router.patch("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
def update_tenant_onboarding(tenant_id: str, payload: TenantOnboardingUpdate, db: Session = Depends(get_db)):
    tenant = _tenant_or_404(tenant_id, db)
    onboarding = tenant.onboarding or _default_onboarding(tenant)
    db.add(onboarding)
    values = payload.model_dump(exclude_unset=True)
    _ensure_status(values.get("onboarding_status"), TENANT_ONBOARDING_STATUSES, "onboarding_status")
    for field, value in values.items():
        setattr(onboarding, field, value)
    onboarding.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(onboarding)
    return onboarding


@router.get("/{tenant_id}/features", response_model=list[TenantFeatureResponse])
@api_router.get("/{tenant_id}/features", response_model=list[TenantFeatureResponse])
def list_tenant_features(tenant_id: str, db: Session = Depends(get_db)):
    _tenant_or_404(tenant_id, db)
    return db.query(TenantFeature).filter(TenantFeature.tenant_id == tenant_id).order_by(TenantFeature.feature_key.asc()).all()


@router.patch("/{tenant_id}/features", response_model=list[TenantFeatureResponse])
@api_router.patch("/{tenant_id}/features", response_model=list[TenantFeatureResponse])
def update_tenant_features(tenant_id: str, payload: list[TenantFeatureUpdate], db: Session = Depends(get_db)):
    _tenant_or_404(tenant_id, db)
    existing = {
        item.feature_key: item
        for item in db.query(TenantFeature).filter(TenantFeature.tenant_id == tenant_id).all()
    }
    for item in payload:
        feature_key = item.feature_key.strip()
        if not feature_key:
            raise HTTPException(status_code=400, detail="feature_key obrigatório.")
        feature = existing.get(feature_key) or TenantFeature(tenant_id=tenant_id, feature_key=feature_key)
        feature.enabled = item.enabled
        feature.limit_value = item.limit_value
        feature.metadata_json = item.metadata_json
        feature.updated_at = datetime.utcnow()
        db.add(feature)
    db.commit()
    return list_tenant_features(tenant_id, db)


@router.get("/{tenant_id}/units", response_model=list[TenantUnitResponse])
@api_router.get("/{tenant_id}/units", response_model=list[TenantUnitResponse])
def list_tenant_units(tenant_id: str, db: Session = Depends(get_db)):
    _tenant_or_404(tenant_id, db)
    return db.query(TenantUnit).filter(TenantUnit.tenant_id == tenant_id).order_by(TenantUnit.created_at.asc()).all()


@router.post("/{tenant_id}/units", response_model=TenantUnitResponse)
@api_router.post("/{tenant_id}/units", response_model=TenantUnitResponse)
def create_tenant_unit(tenant_id: str, payload: TenantUnitCreate, db: Session = Depends(get_db)):
    _tenant_or_404(tenant_id, db)
    _ensure_status(payload.status, TENANT_UNIT_STATUSES, "status")
    unit = TenantUnit(
        tenant_id=tenant_id,
        name=payload.name.strip(),
        status=payload.status,
        city=payload.city,
        state=payload.state,
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit
