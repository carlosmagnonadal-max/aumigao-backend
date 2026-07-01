import os
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.walker_earning import WalkerEarning, WE_ACCRUED

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.models.walker_referral import WalkerReferral
from app.schemas.walker_referral import (
    WALKER_PERFORMANCE_STATUSES,
    WALKER_REFERRAL_STATUSES,
    WALKER_REWARD_STATUSES,
    AdminWalkerReferralStatusUpdate,
    WalkerReferralCreate,
)

BONUS_AFTER_COMPLETED_WALKS = 5
DEFAULT_REWARD_AMOUNT = 20.0


def normalize_phone(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def validate_br_phone(value: str) -> str:
    normalized = normalize_phone(value)
    if len(normalized) not in {10, 11}:
        raise HTTPException(status_code=422, detail="Informe um telefone brasileiro valido com DDD.")
    if len(set(normalized)) <= 2:
        raise HTTPException(status_code=422, detail="Telefone informado parece invalido.")
    return normalized


def can_user_refer_walker(user: User, db: Session) -> bool:
    if user.role in {"tutor", "cliente"}:
        return True
    if user.role in {"walker", "passeador"}:
        profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
        return bool(profile and profile.status in {"approved", "active"})
    return False


def ensure_can_refer(user: User, db: Session) -> None:
    if not can_user_refer_walker(user, db):
        raise HTTPException(status_code=403, detail="Seu perfil ainda nao pode indicar passeadores.")


def generate_referral_code(user: User, db: Session) -> str:
    prefix = (user.id or "USER").replace("-", "")[:6].upper()
    for _ in range(10):
        random_part = uuid4().hex[:6].upper()
        code = f"AUM-{prefix}-{random_part}"
        if not db.query(WalkerReferral).filter(WalkerReferral.referral_code == code).first():
            return code
    raise HTTPException(status_code=500, detail="Nao foi possivel gerar codigo de indicacao.")


def create_walker_referral(payload: WalkerReferralCreate, user: User, db: Session) -> WalkerReferral:
    ensure_can_refer(user, db)
    normalized_phone = validate_br_phone(payload.referred_phone)
    recent_limit = datetime.utcnow() - timedelta(days=7)
    duplicate = (
        db.query(WalkerReferral)
        .filter(
            WalkerReferral.referrer_user_id == user.id,
            WalkerReferral.referred_phone_normalized == normalized_phone,
            WalkerReferral.created_at >= recent_limit,
            WalkerReferral.status.notin_(["cancelled", "rejected"]),
        )
        .first()
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="Voce ja indicou este telefone recentemente.")

    code = generate_referral_code(user, db)
    referral = WalkerReferral(
        id=str(uuid4()),
        referrer_user_id=user.id,
        referred_name=payload.referred_name.strip(),
        referred_phone=payload.referred_phone.strip(),
        referred_phone_normalized=normalized_phone,
        city=payload.city.strip(),
        neighborhood=payload.neighborhood.strip(),
        notes=(payload.notes or "").strip() or None,
        referral_code=code,
        invite_link=_build_invite_link(code),
        status="pending",
        reward_status="not_eligible",
        performance_status="neutral",
    )
    db.add(referral)
    db.commit()
    db.refresh(referral)
    return referral


def validate_referral_code(code: str, db: Session) -> WalkerReferral:
    referral = db.query(WalkerReferral).filter(WalkerReferral.referral_code == code.strip()).first()
    if not referral:
        raise HTTPException(status_code=404, detail="Codigo de indicacao nao encontrado.")
    if referral.status in {"rejected", "cancelled"}:
        raise HTTPException(status_code=409, detail="Esta indicacao nao esta mais disponivel.")
    if referral.referred_user_id:
        raise HTTPException(status_code=409, detail="Esta indicacao ja foi vinculada a um cadastro.")
    return referral


def link_referral_to_user(code: str, user: User, db: Session) -> WalkerReferral:
    referral = validate_referral_code(code, db)
    if referral.referrer_user_id == user.id:
        raise HTTPException(status_code=409, detail="Voce nao pode usar sua propria indicacao.")
    referral.referred_user_id = user.id
    referral.status = "registered"
    referral.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(referral)
    return referral


def mark_referral_under_review(user_id: str, db: Session) -> None:
    referral = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == user_id).first()
    if referral and referral.status in {"registered", "invited", "pending"}:
        referral.status = "under_review"
        referral.updated_at = datetime.utcnow()
        db.commit()


def mark_referral_approved(user_id: str, db: Session, *, commit: bool = True) -> None:
    """Marca referral como aprovado.

    Parâmetro `commit=False` permite incluir a operacao numa transacao maior;
    o chamador fica responsavel por fazer db.commit(). Default True para nao
    quebrar call sites externos.
    """
    referral = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == user_id).first()
    if referral and referral.status in {"registered", "under_review"}:
        referral.status = "approved"
        referral.reward_status = "pending"
        referral.reward_amount = referral.reward_amount or DEFAULT_REWARD_AMOUNT
        referral.approved_at = datetime.utcnow()
        referral.updated_at = datetime.utcnow()
        if commit:
            db.commit()


def mark_referral_rejected(user_id: str, reason: str | None, db: Session, *, commit: bool = True) -> None:
    """Marca referral como rejeitado.

    Parâmetro `commit=False` permite incluir a operacao numa transacao maior;
    o chamador fica responsavel por fazer db.commit(). Default True para nao
    quebrar call sites externos.
    """
    referral = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == user_id).first()
    if referral and referral.status in {"registered", "under_review", "approved"}:
        referral.status = "rejected"
        referral.reward_status = "cancelled"
        referral.rejection_reason = reason
        referral.rejected_at = datetime.utcnow()
        referral.updated_at = datetime.utcnow()
        if commit:
            db.commit()


def update_referral_status(referral: WalkerReferral, payload: AdminWalkerReferralStatusUpdate, db: Session) -> WalkerReferral:
    if payload.status not in WALKER_REFERRAL_STATUSES:
        raise HTTPException(status_code=422, detail="Status de indicacao invalido.")
    if payload.performance_status and payload.performance_status not in WALKER_PERFORMANCE_STATUSES:
        raise HTTPException(status_code=422, detail="Status de performance invalido.")

    referral.status = payload.status
    referral.updated_at = datetime.utcnow()
    if payload.status == "approved":
        referral.approved_at = referral.approved_at or datetime.utcnow()
        referral.reward_status = "pending"
        referral.reward_amount = payload.reward_amount or referral.reward_amount or DEFAULT_REWARD_AMOUNT
    if payload.status == "rejected":
        referral.rejected_at = referral.rejected_at or datetime.utcnow()
        referral.rejection_reason = payload.rejection_reason or referral.rejection_reason
        referral.reward_status = "cancelled"
    if payload.status == "converted":
        referral.converted_at = referral.converted_at or datetime.utcnow()
        referral.reward_status = "eligible"
        referral.reward_amount = payload.reward_amount or referral.reward_amount or DEFAULT_REWARD_AMOUNT
    if payload.status == "cancelled":
        referral.reward_status = "cancelled"

    if payload.reward_amount is not None:
        referral.reward_amount = payload.reward_amount
    if payload.completed_walks_count is not None:
        referral.completed_walks_count = payload.completed_walks_count
        if referral.completed_walks_count >= BONUS_AFTER_COMPLETED_WALKS and referral.status == "approved":
            referral.status = "converted"
            referral.reward_status = "eligible"
            referral.converted_at = referral.converted_at or datetime.utcnow()
            referral.reward_amount = referral.reward_amount or DEFAULT_REWARD_AMOUNT
    if payload.average_rating is not None:
        referral.average_rating = payload.average_rating
    if payload.performance_status:
        referral.performance_status = payload.performance_status
    if referral.reward_status not in WALKER_REWARD_STATUSES:
        referral.reward_status = "not_eligible"

    db.commit()
    db.refresh(referral)
    return referral


PUBLIC_APP_BASE = "https://app.aumigaowalk.com.br"


def _build_invite_link(code: str) -> str:
    return f"{PUBLIC_APP_BASE}/referral/{code}"


def _referral_payout_enabled() -> bool:
    """Gate do payout de referral (dinheiro). Default OFF. Lido em runtime (monkeypatch-safe)."""
    return os.getenv("WALKER_REFERRAL_PAYOUT_ENABLED", "false").lower() in {"true", "1", "yes", "on"}


def pay_referral_rewards(db, referral) -> bool:
    """Credita o bônus de indicação (dois lados) no ledger WalkerEarning.

    Idempotente: guard por reward_status + walk_id sintética única. Gated por
    WALKER_REFERRAL_PAYOUT_ENABLED. Não faz commit (caller comita).
    Retorna True se criou ao menos uma entrada nova.
    """
    if not _referral_payout_enabled():
        return False
    if referral.reward_status not in {"eligible", "pending"}:
        return False

    amount = float(referral.reward_amount or DEFAULT_REWARD_AMOUNT)
    now = datetime.now(timezone.utc)
    sides = [("referrer", referral.referrer_user_id), ("referred", referral.referred_user_id)]
    created = False
    for side, walker_id in sides:
        if not walker_id:
            continue
        synth_walk_id = f"referral-{referral.id}-{side}"
        if db.query(WalkerEarning).filter(WalkerEarning.walk_id == synth_walk_id).first():
            continue
        db.add(WalkerEarning(
            id=str(uuid4()),
            walker_id=walker_id,
            tenant_id=None,
            walk_id=synth_walk_id,
            gross=amount,
            platform_amount=0.0,
            amount=amount,
            status=WE_ACCRUED,
            payable_at=now,
        ))
        created = True

    referral.reward_status = "paid"
    return created


def notify_referral_rewards(db, referral) -> None:
    """Notifica os dois lados que o bônus de indicação foi liberado."""
    from app.models.user import User
    from app.routes.notifications import NotificationCreate, _create_notification

    amount = float(referral.reward_amount or DEFAULT_REWARD_AMOUNT)
    targets = [
        (referral.referrer_user_id, "🎉 Sua indicação converteu",
         f"R$ {amount:.0f} caíram no seu saldo. Obrigado por indicar!"),
        (referral.referred_user_id, "🎉 Você ganhou um bônus",
         f"R$ {amount:.0f} no seu saldo por completar seus primeiros passeios!"),
    ]
    for user_id, title, message in targets:
        if not user_id:
            continue
        user = db.get(User, user_id)
        _create_notification(db, NotificationCreate(
            user_id=user_id,
            user_role=getattr(user, "role", "walker") if user else "walker",
            title=title,
            message=message,
            type="reward_eligible",
            related_entity_type="walker_referral",
            related_entity_id=referral.id,
            metadata={"reward_amount": amount},
        ))


def refresh_referred_walk_count(db, walker_id: str) -> None:
    """Reconta os passeios concluídos do passeador indicado e dispara conversão+payout.

    Idempotente (recompute + guards de estado). Chamada no fluxo de conclusão de passeio.
    """
    if not walker_id:
        return
    from app.constants import WALK_COMPLETED_STATUSES
    from app.models.walk import Walk

    referral = (
        db.query(WalkerReferral)
        .filter(
            WalkerReferral.referred_user_id == walker_id,
            WalkerReferral.status.in_(["approved", "converted"]),
        )
        .first()
    )
    if not referral:
        return

    count = (
        db.query(Walk)
        .filter(
            Walk.walker_id == walker_id,
            Walk.operational_status.in_(list(WALK_COMPLETED_STATUSES)),
        )
        .count()
    )
    referral.completed_walks_count = count

    if count >= BONUS_AFTER_COMPLETED_WALKS and referral.status == "approved":
        referral.status = "converted"
        referral.reward_status = "eligible"
        referral.converted_at = datetime.now(timezone.utc)

    # Pagar quando convertido e ainda elegível (cobre o caso de a flag de payout
    # ter sido ligada DEPOIS da conversão — senão o bônus ficaria preso em 'eligible').
    if referral.status == "converted" and referral.reward_status == "eligible":
        if pay_referral_rewards(db, referral):
            notify_referral_rewards(db, referral)
