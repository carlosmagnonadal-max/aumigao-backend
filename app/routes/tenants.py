from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.models.user import User
from app.services.audit_service import record_audit_log
from app.models.tenant import Tenant, TenantBranding, TenantFeature, TenantSettings, TenantUnit
from app.models.tenant_onboarding import TenantOnboarding
from app.schemas.tenant import (
    TENANT_PLANS,
    TENANT_STATUSES,
    TENANT_UNIT_STATUSES,
    VALID_BACKGROUND_CHECK_PROVIDERS,
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
from app.dependencies.tenant_scope import (
    apply_tenant_filter,
    ensure_tenant_access,
    get_admin_tenant_scope,
    is_super_admin,
)
from app.services.tenant_plan_service import (
    DEFAULT_ON_FEATURE_KEYS,
    enforce_can_add_tenant_unit,
    enforce_plan_allows_product_feature,
    enforce_tenant_feature_allowed,
    get_tenant_capabilities,
)

router = APIRouter(prefix="/admin/tenants", tags=["admin-tenants"], dependencies=[Depends(require_permission("tenants.read"))])
api_router = APIRouter(prefix="/api/admin/tenants", tags=["admin-tenants"], dependencies=[Depends(require_permission("tenants.read"))])


def _scope_or_404(admin: User, tenant_id: str, db: Session) -> None:
    """Isolamento multi-tenant (Onda 1 / mt-MT1+MT5): super_admin acessa qualquer
    tenant; admin de tenant só o PRÓPRIO. Cross-tenant -> 404 (não vaza existência).
    Mesma regra já aplicada em update_tenant_features (D5), agora em todos os endpoints.
    """
    ensure_tenant_access(tenant_id, get_admin_tenant_scope(admin, db))


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


def _list_features(tenant_id: str, db: Session):
    return db.query(TenantFeature).filter(TenantFeature.tenant_id == tenant_id).order_by(TenantFeature.feature_key.asc()).all()


@router.get("", response_model=list[TenantResponse])
@api_router.get("", response_model=list[TenantResponse])
def list_tenants(admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Escopo: super_admin vê todos; admin de tenant vê só o próprio (Onda 1).
    scope = get_admin_tenant_scope(admin, db)
    query = db.query(Tenant).order_by(Tenant.created_at.desc())
    query = apply_tenant_filter(query, Tenant, scope, tenant_column=Tenant.id)
    return query.all()


@router.post("", response_model=TenantDetailResponse)
@api_router.post("", response_model=TenantDetailResponse)
def create_tenant(payload: TenantCreate, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Criar um novo tenant é ação de plataforma — apenas super_admin (Onda 1).
    if not is_super_admin(admin):
        raise HTTPException(status_code=403, detail="Apenas super_admin pode criar tenants.")
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
    record_audit_log(
        db, action="tenant.created", entity_type="tenant", entity_id=tenant.id, actor=admin,
        after={"name": tenant.name, "slug": tenant.slug, "plan": tenant.plan}, tenant_id=tenant.id,
    )
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/{tenant_id}", response_model=TenantDetailResponse)
@api_router.get("/{tenant_id}", response_model=TenantDetailResponse)
def get_tenant(tenant_id: str, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    return _tenant_or_404(tenant_id, db)


@router.patch("/{tenant_id}", response_model=TenantResponse)
@api_router.patch("/{tenant_id}", response_model=TenantResponse)
def update_tenant(tenant_id: str, payload: TenantUpdate, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)
    values = payload.model_dump(exclude_unset=True)
    _ensure_status(values.get("status"), TENANT_STATUSES, "status")
    _ensure_plan(values.get("plan"))
    for field, value in values.items():
        setattr(tenant, field, value.strip() if isinstance(value, str) else value)
    tenant.updated_at = datetime.utcnow()
    # Mudança de plano atualiza a comissão para o default do novo tier — exceto quando
    # a comissão foi negociada à mão (commission_is_custom), que prevalece.
    if "plan" in values:
        from app.models.tenant_payment_config import commission_default_for_plan
        from app.services.payment_split_service import get_or_create_payment_config

        pay_cfg = get_or_create_payment_config(db, tenant.id)
        if not pay_cfg.commission_is_custom:
            pay_cfg.commission_percent = commission_default_for_plan(tenant.plan)
    record_audit_log(
        db, action="tenant.updated", entity_type="tenant", entity_id=tenant.id, actor=admin,
        after=values, tenant_id=tenant.id,
    )
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/{tenant_id}/branding", response_model=TenantBrandingResponse)
@api_router.get("/{tenant_id}/branding", response_model=TenantBrandingResponse)
def get_tenant_branding(tenant_id: str, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)
    if not tenant.branding:
        tenant.branding = _default_branding(tenant)
        db.commit()
        db.refresh(tenant.branding)
    return tenant.branding


@router.patch("/{tenant_id}/branding", response_model=TenantBrandingResponse)
@api_router.patch("/{tenant_id}/branding", response_model=TenantBrandingResponse)
def update_tenant_branding(tenant_id: str, payload: TenantBrandingUpdate, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
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
def get_tenant_settings(tenant_id: str, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)
    if not tenant.settings:
        tenant.settings = _default_settings(tenant)
        db.commit()
        db.refresh(tenant.settings)
    return tenant.settings


@router.patch("/{tenant_id}/settings", response_model=TenantSettingsResponse)
@api_router.patch("/{tenant_id}/settings", response_model=TenantSettingsResponse)
def update_tenant_settings(tenant_id: str, payload: TenantSettingsUpdate, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)
    settings = tenant.settings or _default_settings(tenant)
    db.add(settings)
    values = payload.model_dump(exclude_unset=True)
    # Valida background_check_provider contra a lista de valores permitidos.
    # TODO: ao implementar provedor pago, exigir background_check_provider_config
    #       presente e cifrar credenciais com Fernet/KMS antes de salvar.
    if "background_check_provider" in values:
        provider_val = (values["background_check_provider"] or "").strip().lower()
        if provider_val not in VALID_BACKGROUND_CHECK_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Valor invalido para background_check_provider: '{provider_val}'. "
                    f"Valores permitidos: {list(VALID_BACKGROUND_CHECK_PROVIDERS)}."
                ),
            )
        values["background_check_provider"] = provider_val
    for field, value in values.items():
        setattr(settings, field, value.strip() if isinstance(value, str) else value)
    settings.updated_at = datetime.utcnow()
    record_audit_log(
        db, action="settings.updated", entity_type="tenant_settings", entity_id=tenant_id, actor=admin,
        after=payload.model_dump(exclude_unset=True), tenant_id=tenant_id,
    )
    db.commit()
    db.refresh(settings)
    return settings


@router.get("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
@api_router.get("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
def get_tenant_onboarding(tenant_id: str, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)
    return _ensure_tenant_onboarding(tenant, db)


@router.get("/{tenant_id}/capabilities", response_model=TenantCapabilitiesResponse)
@api_router.get("/{tenant_id}/capabilities", response_model=TenantCapabilitiesResponse)
def get_tenant_capabilities_endpoint(tenant_id: str, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)
    return {
        "tenant_id": tenant.id,
        "plan": tenant.plan,
        "capabilities": get_tenant_capabilities(tenant, db),
    }


@router.patch("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
@api_router.patch("/{tenant_id}/onboarding", response_model=TenantOnboardingResponse)
def update_tenant_onboarding(tenant_id: str, payload: TenantOnboardingUpdate, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
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
def list_tenant_features(tenant_id: str, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    _tenant_or_404(tenant_id, db)
    return _list_features(tenant_id, db)


@router.patch("/{tenant_id}/features", response_model=list[TenantFeatureResponse])
@api_router.patch("/{tenant_id}/features", response_model=list[TenantFeatureResponse])
def update_tenant_features(tenant_id: str, payload: list[TenantFeatureUpdate], admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)

    existing = {
        item.feature_key: item
        for item in db.query(TenantFeature).filter(TenantFeature.tenant_id == tenant_id).all()
    }
    for item in payload:
        feature_key = item.feature_key.strip()
        if not feature_key:
            raise HTTPException(status_code=400, detail="feature_key obrigatório.")
        if item.enabled:
            enforce_tenant_feature_allowed(tenant, db, feature_key)
            enforce_plan_allows_product_feature(tenant, feature_key)
        feature = existing.get(feature_key) or TenantFeature(tenant_id=tenant_id, feature_key=feature_key)
        feature.enabled = item.enabled
        feature.limit_value = item.limit_value
        feature.metadata_json = item.metadata_json
        feature.updated_at = datetime.utcnow()
        db.add(feature)
    record_audit_log(
        db, action="features.updated", entity_type="tenant_features", entity_id=tenant_id, actor=admin,
        after={"features": [{"key": i.feature_key, "enabled": i.enabled} for i in payload]}, tenant_id=tenant_id,
    )
    db.commit()
    return _list_features(tenant_id, db)


@router.get("/{tenant_id}/units", response_model=list[TenantUnitResponse])
@api_router.get("/{tenant_id}/units", response_model=list[TenantUnitResponse])
def list_tenant_units(tenant_id: str, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    _tenant_or_404(tenant_id, db)
    return db.query(TenantUnit).filter(TenantUnit.tenant_id == tenant_id).order_by(TenantUnit.created_at.asc()).all()


@router.post("/{tenant_id}/units", response_model=TenantUnitResponse)
@api_router.post("/{tenant_id}/units", response_model=TenantUnitResponse)
def create_tenant_unit(tenant_id: str, payload: TenantUnitCreate, admin: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _scope_or_404(admin, tenant_id, db)
    tenant = _tenant_or_404(tenant_id, db)
    _ensure_status(payload.status, TENANT_UNIT_STATUSES, "status")
    enforce_can_add_tenant_unit(tenant, db)
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
