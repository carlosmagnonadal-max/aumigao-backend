"""Regras de negócio dos passeios compartilhados (Onda 1).

Foco desta entrega: CONVITE (host cria com os próprios pets; outro tutor entra
com o pet dele) + caso "mesmo tutor" (vários cães próprios). Pool automático fica
atrás do toggle `pool_enabled` (default off). Ver memória passeios-compartilhados.

Pagamento segue a maturidade atual do beta (registro de Payment + checkout);
a confirmação real no gateway é o mesmo pendente do Sprint 16.
"""
from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.payment import Payment
from app.models.pet import Pet
from app.models.shared_walk import (
    ORIGIN_INVITE,
    ORIGIN_POOL,
    PARTICIPANT_ACCEPTED,
    PARTICIPANT_CANCELLED,
    PARTICIPANT_PAID,
    SHARED_CANCELLED,
    SHARED_CONFIRMED,
    SHARED_FORMING,
    SHARED_WALKS_FEATURE_KEY,
    SharedWalk,
    SharedWalkParticipant,
    TenantSharedWalkConfig,
)
from app.models.tenant import Tenant
from app.services.tenant_plan_service import enforce_tenant_product_feature, tenant_has_feature

FEATURE_LABEL = "Passeios compartilhados"
ACTIVE_PARTICIPANT_STATUSES = {PARTICIPANT_ACCEPTED, PARTICIPANT_PAID}


def shared_walks_enabled(tenant: Tenant, db: Session) -> bool:
    return tenant_has_feature(tenant, db, SHARED_WALKS_FEATURE_KEY)


def enforce_enabled(tenant: Tenant, db: Session) -> None:
    enforce_tenant_product_feature(tenant, db, SHARED_WALKS_FEATURE_KEY, FEATURE_LABEL)


def get_or_create_config(db: Session, tenant_id: str) -> TenantSharedWalkConfig:
    config = (
        db.query(TenantSharedWalkConfig)
        .filter(TenantSharedWalkConfig.tenant_id == tenant_id)
        .first()
    )
    if not config:
        config = TenantSharedWalkConfig(tenant_id=tenant_id)
        db.add(config)
        db.flush()
    return config


def get_session_or_404(db: Session, tenant_id: str, walk_id: str) -> SharedWalk:
    session = (
        db.query(SharedWalk)
        .filter(SharedWalk.tenant_id == tenant_id, SharedWalk.id == walk_id)
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Passeio compartilhado não encontrado.")
    return session


def active_participants(session: SharedWalk) -> list[SharedWalkParticipant]:
    return [p for p in session.participants if p.status in ACTIVE_PARTICIPANT_STATUSES]


def tutor_count(session: SharedWalk) -> int:
    return len({p.tutor_id for p in active_participants(session)})


def _pet_owned(db: Session, pet_id: str, tutor_id: str) -> Pet:
    pet = db.get(Pet, pet_id)
    if not pet or pet.tutor_id != tutor_id:
        raise HTTPException(status_code=404, detail="Pet não encontrado.")
    return pet


def create_session(
    db: Session,
    tenant: Tenant,
    host_tutor_id: str,
    *,
    scheduled_date: str,
    duration_minutes: int,
    host_pet_ids: list[str],
    open_to_pool: bool,
) -> SharedWalk:
    enforce_enabled(tenant, db)
    config = get_or_create_config(db, tenant.id)
    if not config.active:
        raise HTTPException(status_code=409, detail="Passeios compartilhados indisponíveis no momento.")

    unique_pets = list(dict.fromkeys(host_pet_ids))
    if not unique_pets:
        raise HTTPException(status_code=400, detail="Escolha ao menos um pet.")
    if len(unique_pets) > config.max_pets_same_tutor:
        raise HTTPException(status_code=400, detail=f"Máximo de {config.max_pets_same_tutor} pets do mesmo tutor.")
    for pet_id in unique_pets:
        _pet_owned(db, pet_id, host_tutor_id)

    # Pool só quando o tenant habilitou; senão a sessão é por convite.
    pool = bool(open_to_pool and config.pool_enabled)
    session = SharedWalk(
        tenant_id=tenant.id,
        created_by_tutor_id=host_tutor_id,
        status=SHARED_FORMING,
        origin=ORIGIN_POOL if pool else ORIGIN_INVITE,
        scheduled_date=scheduled_date,
        duration_minutes=duration_minutes,
        price_per_pet=config.price_per_pet,
        max_tutors=config.max_tutors,
        open_to_pool=pool,
    )
    db.add(session)
    db.flush()
    for pet_id in unique_pets:
        db.add(SharedWalkParticipant(
            shared_walk_id=session.id,
            tutor_id=host_tutor_id,
            pet_id=pet_id,
            role="host",
            status=PARTICIPANT_ACCEPTED,
            price=config.price_per_pet,
        ))
    db.commit()
    db.refresh(session)
    return session


def join_session(db: Session, tenant: Tenant, walk_id: str, guest_tutor_id: str, pet_id: str) -> SharedWalk:
    enforce_enabled(tenant, db)
    session = get_session_or_404(db, tenant.id, walk_id)
    if session.status != SHARED_FORMING:
        raise HTTPException(status_code=409, detail="Este passeio não está mais aceitando participantes.")

    existing_tutors = {p.tutor_id for p in active_participants(session)}
    if guest_tutor_id not in existing_tutors and len(existing_tutors) >= session.max_tutors:
        raise HTTPException(status_code=409, detail="O grupo já atingiu o limite de tutores.")

    pet = _pet_owned(db, pet_id, guest_tutor_id)
    # Compatibilidade entre tutores: o pet precisa poder passear com outros pets.
    if guest_tutor_id != session.created_by_tutor_id and not pet.can_walk_with_other_pets:
        raise HTTPException(status_code=400, detail="Este pet não está habilitado para passeio com outros pets.")
    if any(p.pet_id == pet_id and p.status in ACTIVE_PARTICIPANT_STATUSES for p in session.participants):
        raise HTTPException(status_code=409, detail="Este pet já está no passeio.")

    db.add(SharedWalkParticipant(
        shared_walk_id=session.id,
        tutor_id=guest_tutor_id,
        pet_id=pet_id,
        role="guest",
        status=PARTICIPANT_ACCEPTED,
        price=session.price_per_pet,
    ))
    db.commit()
    db.refresh(session)
    return session


def checkout(db: Session, tenant: Tenant, walk_id: str, tutor_id: str) -> SharedWalk:
    """Tutor paga a cota dos seus pets (1 pagamento por tutor). Marca participantes PAID."""
    session = get_session_or_404(db, tenant.id, walk_id)
    mine = [p for p in session.participants if p.tutor_id == tutor_id and p.status == PARTICIPANT_ACCEPTED]
    if not mine:
        raise HTTPException(status_code=400, detail="Nenhuma cota pendente para este tutor.")
    amount = sum(p.price for p in mine)
    payment = Payment(
        id=str(uuid4()),
        tenant_id=tenant.id,
        tutor_id=tutor_id,
        amount=amount,
        status="pending",
        provider="internal",
    )
    db.add(payment)
    for p in mine:
        p.status = PARTICIPANT_PAID
        p.payment_id = payment.id
        p.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(session)
    return session


def confirm_session(db: Session, tenant: Tenant, walk_id: str, host_tutor_id: str) -> SharedWalk:
    session = get_session_or_404(db, tenant.id, walk_id)
    if session.created_by_tutor_id != host_tutor_id:
        raise HTTPException(status_code=403, detail="Só quem criou pode confirmar o passeio.")
    if session.status != SHARED_FORMING:
        raise HTTPException(status_code=409, detail="Passeio já confirmado ou cancelado.")
    parts = active_participants(session)
    if any(p.status != PARTICIPANT_PAID for p in parts):
        raise HTTPException(status_code=409, detail="Aguardando o pagamento de todos os participantes.")
    # Compartilhado exige >= 2 pets (do mesmo tutor) ou >= 2 tutores.
    if len(parts) < 2:
        raise HTTPException(status_code=409, detail="Um passeio compartilhado precisa de ao menos 2 pets.")
    session.status = SHARED_CONFIRMED
    session.confirmed_at = datetime.utcnow()
    db.commit()
    db.refresh(session)
    return session


def cancel_participation(db: Session, tenant: Tenant, walk_id: str, tutor_id: str) -> SharedWalk:
    session = get_session_or_404(db, tenant.id, walk_id)
    if tutor_id == session.created_by_tutor_id:
        # Host cancela a sessão inteira.
        session.status = SHARED_CANCELLED
        for p in session.participants:
            p.status = PARTICIPANT_CANCELLED
    else:
        for p in session.participants:
            if p.tutor_id == tutor_id:
                p.status = PARTICIPANT_CANCELLED
    db.commit()
    db.refresh(session)
    return session


def list_my_sessions(db: Session, tenant_id: str, tutor_id: str) -> list[SharedWalk]:
    rows = (
        db.query(SharedWalk)
        .join(SharedWalkParticipant, SharedWalkParticipant.shared_walk_id == SharedWalk.id)
        .filter(SharedWalk.tenant_id == tenant_id, SharedWalkParticipant.tutor_id == tutor_id)
        .order_by(SharedWalk.created_at.desc())
        .all()
    )
    # Dedup mantendo ordem.
    seen: set[str] = set()
    out: list[SharedWalk] = []
    for r in rows:
        if r.id not in seen:
            seen.add(r.id)
            out.append(r)
    return out
