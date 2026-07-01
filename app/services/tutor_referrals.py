from __future__ import annotations

import os
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tenant import Tenant
from app.models.user import User
from app.models.tutor_referral import TutorReferral

PUBLIC_APP_BASE = "https://app.aumigaowalk.com.br"


def _payout_enabled() -> bool:
    """Gate do payout de indicação do tutor (dinheiro). Default OFF. Lido em runtime."""
    return os.getenv("TUTOR_REFERRAL_PAYOUT_ENABLED", "false").lower() in {"true", "1", "yes", "on"}


def _build_invite_link(code: str) -> str:
    return f"{PUBLIC_APP_BASE}/tutor-referral/{code}"


def generate_tutor_referral_code(user: User, db: Session) -> str:
    prefix = (user.id or "USER").replace("-", "")[:6].upper()
    for _ in range(10):
        code = f"TUT-{prefix}-{uuid4().hex[:6].upper()}"
        if not db.query(TutorReferral).filter(TutorReferral.referral_code == code).first():
            return code
    raise HTTPException(status_code=500, detail="Não foi possível gerar código de indicação.")


def create_tutor_referral(db: Session, referrer: User, tenant_id: str) -> TutorReferral:
    """Cria (ou retorna o pendente existente) o referral do tutor no tenant."""
    existing = (
        db.query(TutorReferral)
        .filter(
            TutorReferral.tenant_id == tenant_id,
            TutorReferral.referrer_user_id == referrer.id,
            TutorReferral.status == "pending",
        )
        .first()
    )
    if existing:
        return existing
    code = generate_tutor_referral_code(referrer, db)
    ref = TutorReferral(
        id=str(uuid4()),
        tenant_id=tenant_id,
        referrer_user_id=referrer.id,
        referral_code=code,
        invite_link=_build_invite_link(code),
        status="pending",
        reward_status="not_eligible",
    )
    db.add(ref)
    db.commit()
    db.refresh(ref)
    return ref


def validate_tutor_referral_code(db: Session, code: str) -> dict:
    """Dados públicos sanitizados p/ a landing. 404 se inexistente/cancelado."""
    ref = db.query(TutorReferral).filter(TutorReferral.referral_code == code).first()
    if not ref or ref.status == "cancelled":
        raise HTTPException(status_code=404, detail="Código de indicação inválido.")
    tenant = db.get(Tenant, ref.tenant_id)
    referrer = db.get(User, ref.referrer_user_id)
    first_name = ((getattr(referrer, "full_name", None) or "").split(" ")[0]) if referrer else ""
    return {
        "tenant_id": ref.tenant_id,
        "tenant_name": getattr(tenant, "name", None),
        "tenant_slug": getattr(tenant, "slug", None),
        "referrer_first_name": first_name,
    }


def link_tutor_referral(db: Session, code: str, referred_user_id: str, tenant_id: str) -> TutorReferral:
    """Liga o convidado ao referral (status registered). Idempotente; bloqueia auto-indicação."""
    ref = db.query(TutorReferral).filter(TutorReferral.referral_code == code).first()
    if not ref or ref.tenant_id != tenant_id or ref.status == "cancelled":
        raise HTTPException(status_code=404, detail="Código de indicação inválido.")
    if ref.referrer_user_id == referred_user_id:
        raise HTTPException(status_code=422, detail="Não é possível usar a própria indicação.")
    if ref.referred_user_id and ref.referred_user_id != referred_user_id:
        raise HTTPException(status_code=409, detail="Código já utilizado por outro tutor.")
    ref.referred_user_id = referred_user_id
    if ref.status == "pending":
        ref.status = "registered"
    db.commit()
    db.refresh(ref)
    return ref
