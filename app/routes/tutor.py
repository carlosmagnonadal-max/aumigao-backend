from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db, get_tutor_self_db
from app.core.feature_flags import multi_tenant_tutor_enabled
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.tenant_tutor_access import TenantTutorAccess
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.schemas.tutor_profile import TutorProfileCreate, TutorProfileResponse, TutorProfileUpdate
from app.services.identity_uniqueness import ensure_unique_identity
from app.services.tenant_context import resolve_current_tenant_id
from app.utils.registration_validation import normalize_cpf_or_raise, normalize_phone_or_raise

router = APIRouter(prefix="/tutor", tags=["tutor"])


def _normalized_profile_payload(payload: TutorProfileCreate | TutorProfileUpdate):
    data = payload.model_dump()
    try:
        if "cpf" in data and data.get("cpf"):
            data["cpf"] = normalize_cpf_or_raise(data.get("cpf"))
        if "phone" in data and data.get("phone"):
            data["phone"] = normalize_phone_or_raise(data.get("phone"))
    except ValueError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc))
    return data


def _current_tenant_id(user: User, db: Session) -> str:
    tenant_id = user.tenant_id or resolve_current_tenant_id(db)
    if not user.tenant_id:
        user.tenant_id = tenant_id
    return tenant_id


def _profile_query(user: User, db: Session, tenant_id: str):
    return db.query(TutorProfile).filter(
        TutorProfile.user_id == user.id,
        or_(TutorProfile.tenant_id == tenant_id, TutorProfile.tenant_id.is_(None)),
    )


@router.get("/profile", response_model=TutorProfileResponse | None)
def get_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = _current_tenant_id(user, db)
    profile = _profile_query(user, db, tenant_id).first()
    if profile and not profile.tenant_id:
        profile.tenant_id = tenant_id
        db.commit()
        db.refresh(profile)
    return profile


@router.post("/profile", response_model=TutorProfileResponse)
def create_profile(payload: TutorProfileCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = _current_tenant_id(user, db)
    profile = _profile_query(user, db, tenant_id).first()
    if profile:
        return update_profile(payload, user, db)
    data = _normalized_profile_payload(payload)
    ensure_unique_identity(db, cpf=data.get("cpf") or None, phone=data.get("phone") or None, current_user_id=user.id)
    profile = TutorProfile(id=str(uuid4()), user_id=user.id, tenant_id=tenant_id, **data)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.put("/profile", response_model=TutorProfileResponse)
def update_profile(payload: TutorProfileUpdate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant_id = _current_tenant_id(user, db)
    profile = _profile_query(user, db, tenant_id).first()
    if not profile:
        profile = TutorProfile(id=str(uuid4()), user_id=user.id, tenant_id=tenant_id)
        db.add(profile)
    elif not profile.tenant_id:
        profile.tenant_id = tenant_id
    data = _normalized_profile_payload(payload)
    ensure_unique_identity(db, cpf=data.get("cpf") or None, phone=data.get("phone") or None, current_user_id=user.id)
    for key, value in data.items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return profile


# ─── Modelo B — Multi-Tenant Tutor ───────────────────────────────────────────


class TutorJoinRequest(BaseModel):
    tenant_slug: str
    referral_code: str | None = None


def _tenant_brand_dict(tenant: Tenant, status: str) -> dict:
    b = tenant.branding
    return {
        "tenant_id": tenant.id,
        "slug": tenant.slug,
        "display_name": (b.display_name if b and b.display_name else tenant.name),
        "brand_color": (b.primary_color if b else None),
        "logo_url": (b.logo_url if b else None),
        "access_status": status,
    }


@router.get("/tenants")
def tutor_tenants(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_tutor_self_db),
):
    """Tenants em que o tutor está ATIVO (status=active) — com branding. Modelo B."""
    if not multi_tenant_tutor_enabled():
        return []
    accesses = (
        db.query(TenantTutorAccess)
        .filter(TenantTutorAccess.tutor_user_id == user.id, TenantTutorAccess.status == "active")
        .order_by(TenantTutorAccess.created_at.desc())
        .all()
    )
    out: list[dict] = []
    seen: set[str] = set()
    # Tenant NATIVO do tutor entra como vínculo implícito ativo, em primeiro —
    # tutor do tenant vê a marca dele sem join manual (incidente 11/07: o logo
    # do white label não chegava ao app porque a lista vinha vazia e o app não
    # tinha o que auto-ativar).
    if user.tenant_id:
        home = db.get(Tenant, user.tenant_id)
        if home:
            out.append(_tenant_brand_dict(home, "active"))
            seen.add(home.id)
    for a in accesses:
        if a.tenant_id in seen:
            continue
        tenant = db.get(Tenant, a.tenant_id)
        if tenant:
            out.append(_tenant_brand_dict(tenant, a.status))
            seen.add(a.tenant_id)
    return out


@router.post("/tenants/join")
def tutor_join_tenant(
    payload: TutorJoinRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_tutor_self_db),
):
    """Cria ou reativa vínculo tutor↔tenant. Idempotente. Modelo B."""
    if not multi_tenant_tutor_enabled():
        raise HTTPException(status_code=404, detail="multi_tenant_tutor_disabled")
    tenant = (
        db.query(Tenant)
        .filter(Tenant.slug == payload.tenant_slug, Tenant.status == "active")
        .first()
    )
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant_not_found")
    existing = (
        db.query(TenantTutorAccess)
        .filter(TenantTutorAccess.tenant_id == tenant.id, TenantTutorAccess.tutor_user_id == user.id)
        .first()
    )
    if existing:
        if existing.status != "active":
            existing.status = "active"
            db.commit()
        access = existing
    else:
        access = TenantTutorAccess(
            tenant_id=tenant.id,
            tutor_user_id=user.id,
            status="active",
            initiated_by="tutor",
        )
        db.add(access)
        db.commit()
    # Growth loop cunha 4: se veio código de indicação, liga o convidado ao referral.
    if payload.referral_code:
        try:
            from app.services.tutor_referrals import link_tutor_referral, refresh_referral_conversion
            link_tutor_referral(db, payload.referral_code, user.id, tenant.id)
            # gatilho "no_cadastro" converte já na entrada; os demais convertem na conclusão do passeio.
            refresh_referral_conversion(db, user.id, tenant.id)
        except Exception:
            import logging
            logging.getLogger("aumigao.tutor").warning(
                "falha ao ligar referral do tutor code=%s user=%s", payload.referral_code, user.id
            )
    return _tenant_brand_dict(tenant, access.status)
