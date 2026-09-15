"""Botão de Emergência (S1) — acionamento pelo passeador com o cão sob custódia.

Spec: docs/superpowers/specs/2026-09-15-seguranca-do-passeio-design.md §1 (D3/D4).
Plano: docs/superpowers/plans/2026-09-15-s1-botao-emergencia.md (desvios DV1-DV14).

Regras:
- Só o passeador DESIGNADO (walk.walker_id ou walk.assigned_walker_id == user.id).
- Só com o cão sob custódia: operational_status em CUSTODY_STATUSES (403 fora).
- Rate-limit por passeio: máx. RATE_LIMIT_MAX_CALLS acionamentos em
  RATE_LIMIT_WINDOW; o excedente NÃO grava nada e devolve o último (rate_limited).
- A resposta (número para discar) NUNCA depende da persistência: se gravar falhar,
  rollback + log e a resposta sai com persisted=False. Falha de notificação não
  desfaz o registro.
- O telefone do tutor só sai NESTA resposta e só em modo direto. Nenhum evento,
  notificação ou log carrega o número.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.tenant import Tenant, TenantSettings
from app.models.tutor_profile import TutorProfile
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_emergency_call import WalkEmergencyCall
from app.models.walker_profile import WalkerProfile
from app.services.operational_reliability_service import EMERGENCY_CALL, create_operational_event
from app.services.telephony import get_telephony_provider, to_e164_br

LOGGER = logging.getLogger("aumigao.walk_emergency")

CUSTODY_STATUSES = frozenset({"pet_handover_confirmed", "ride_in_progress"})
RATE_LIMIT_MAX_CALLS = 3
RATE_LIMIT_WINDOW = timedelta(minutes=10)
EMERGENCY_REASONS = ("pet_mal", "fuga", "ataque", "outro")
REASON_LABELS = {
    "pet_mal": "pet passando mal",
    "fuga": "fuga do pet",
    "ataque": "ataque ou briga",
    "outro": "outro motivo",
}
MODE_LABELS = {
    "direct": "direta pelo celular do passeador",
    "masked": "mascarada via central",
    "unavailable": "tutor sem telefone cadastrado",
}
TUTOR_NOTIFICATION_TYPE = "walk_emergency"
ADMIN_NOTIFICATION_TYPE = "walk_emergency_admin"


@dataclass(frozen=True)
class _CallPlan:
    mode: str
    status: str
    provider: str
    provider_call_id: str | None
    fallback_reason: str | None
    tutor_e164: str | None


def is_walk_in_custody(walk: Walk) -> bool:
    return str(walk.operational_status or "").strip() in CUSTODY_STATUSES


def _is_designated_walker(walk: Walk, user: User) -> bool:
    return bool(user and user.id) and user.id in {walk.walker_id, walk.assigned_walker_id}


def _active_tenant(db: Session) -> str | None:
    tenant = db.info.get("rls_tenant")
    return tenant if tenant and tenant not in ("*", "") else None


def _tutor_e164(db: Session, tutor_id: str | None) -> str | None:
    if not tutor_id:
        return None
    profile = db.query(TutorProfile).filter(TutorProfile.user_id == tutor_id).first()
    return to_e164_br(profile.phone) if profile else None


def _walker_e164(db: Session, user_id: str) -> str | None:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user_id).first()
    return to_e164_br(profile.phone) if profile else None


def _tenant_support_phone(db: Session, tenant_id: str | None) -> str | None:
    if not tenant_id:
        return None
    settings = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
    raw = (settings.support_phone if settings else None) or ""
    if not raw.strip():
        tenant = db.get(Tenant, tenant_id)
        raw = (tenant.contact_phone if tenant else None) or ""
    raw = raw.strip()
    if not raw:
        return None
    return to_e164_br(raw) or raw


def _plan_call(db: Session, walk_id: str, tutor_id: str | None, walker_user_id: str) -> _CallPlan:
    provider = get_telephony_provider()
    tutor_e164 = _tutor_e164(db, tutor_id)
    if not tutor_e164:
        return _CallPlan("unavailable", "failed", provider.name, None, "tutor_phone_missing", None)
    if not provider.is_enabled():
        return _CallPlan("direct", "initiated", provider.name, None, None, tutor_e164)
    walker_e164 = _walker_e164(db, walker_user_id)
    if not walker_e164:
        return _CallPlan("direct", "fallback_direct", provider.name, None, "walker_phone_missing", tutor_e164)
    try:
        result = provider.start_bridge_call(walker_e164, tutor_e164, os.getenv("TELEPHONY_CALLER_ID") or None)
    except Exception:  # noqa: BLE001 — provedor externo nunca derruba a emergência
        LOGGER.exception("walk_emergency: provedor %s falhou walk_id=%s", provider.name, walk_id)
        return _CallPlan("direct", "fallback_direct", provider.name, None, "provider_exception", tutor_e164)
    if result.ok and result.provider_call_id:
        return _CallPlan("masked", "initiated", provider.name, result.provider_call_id, None, tutor_e164)
    return _CallPlan("direct", "fallback_direct", provider.name, None, result.error or "provider_failed", tutor_e164)


def _event_notes(reason: str | None, mode: str) -> str:
    reason_label = REASON_LABELS.get(reason or "", "não informado")
    return (
        "Botão de emergência acionado pelo passeador. "
        f"Motivo: {reason_label}. Ligação: {MODE_LABELS.get(mode, mode)}."
    )


def _notify_tutor(
    db: Session, *, walk_id: str, tutor_id: str | None, tenant_id: str | None,
    pet_name: str, reason: str | None, call_id: str,
) -> None:
    from app.routes.notifications import NotificationCreate, _create_notification

    if not tutor_id:
        return
    _create_notification(
        db,
        NotificationCreate(
            tenant_id=tenant_id,
            user_id=tutor_id,
            user_role="tutor",
            title=f"Emergência no passeio de {pet_name}",
            message="O passeador acionou o botão de emergência e está ligando para você. Atenda o telefone.",
            type=TUTOR_NOTIFICATION_TYPE,
            related_entity_type="walk",
            related_entity_id=walk_id,
            metadata={"walk_id": walk_id, "call_id": call_id, "reason": reason or "", "priority": "high"},
        ),
    )


def _notify_admins(
    db: Session, *, walk_id: str, tenant_id: str | None,
    pet_name: str, reason: str | None, call_id: str,
) -> None:
    from app.routes.notifications import NotificationCreate, _create_notification

    query = db.query(User).filter(User.role.in_(("admin", "super_admin")))
    if tenant_id:
        query = query.filter(User.tenant_id == tenant_id)
    else:
        query = query.filter(User.role == "super_admin")
    reason_label = REASON_LABELS.get(reason or "", "motivo não informado")
    for admin in query.all():
        _create_notification(
            db,
            NotificationCreate(
                tenant_id=tenant_id,
                user_id=admin.id,
                user_role=admin.role,
                title=f"Emergência no passeio de {pet_name}",
                message=f"O passeador acionou o botão de emergência ({reason_label}). Acompanhe o passeio agora.",
                type=ADMIN_NOTIFICATION_TYPE,
                related_entity_type="walk",
                related_entity_id=walk_id,
                metadata={
                    "walk_id": walk_id, "call_id": call_id, "reason": reason or "",
                    "priority": "high", "channel": "in_app",
                },
            ),
        )


def _response(
    *, call_id: str | None, walk_id: str, mode: str, status: str, tutor_e164: str | None,
    support_phone: str | None, vet_name: str | None, vet_phone: str | None,
    created_at: datetime | None, rate_limited: bool, persisted: bool,
) -> dict:
    return {
        "call_id": call_id,
        "walk_id": walk_id,
        "mode": mode,
        "status": status,
        "tutor_phone_e164": tutor_e164 if mode == "direct" else None,
        "tenant_support_phone": support_phone,
        "pet_vet_name": vet_name,
        "pet_vet_phone": vet_phone,
        "created_at": created_at.isoformat() if created_at else None,
        "rate_limited": rate_limited,
        "persisted": persisted,
    }


def trigger_walk_emergency(db: Session, walk: Walk, user: User, reason: str | None) -> dict:
    if not _is_designated_walker(walk, user):
        raise HTTPException(status_code=403, detail="Passeio nao pertence ao passeador")
    if not is_walk_in_custody(walk):
        raise HTTPException(
            status_code=403,
            detail="Emergencia disponivel apenas com o pet sob sua custodia (da entrega ate o fim do passeio).",
        )
    if reason is not None and reason not in EMERGENCY_REASONS:
        raise HTTPException(status_code=422, detail="Motivo de emergencia invalido.")

    # Escalares capturados ANTES de qualquer commit/rollback (objetos ORM expiram).
    now = datetime.utcnow()
    walk_id = walk.id
    tutor_id = walk.tutor_id
    walker_user_id = user.id
    tenant_id = walk.tenant_id or _active_tenant(db)
    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    pet_name = pet.name if pet and pet.name else "seu pet"
    vet_name = (pet.vet_name or None) if pet else None
    vet_phone = (pet.vet_phone or None) if pet else None
    support_phone = _tenant_support_phone(db, tenant_id)

    recent = (
        db.query(WalkEmergencyCall)
        .filter(
            WalkEmergencyCall.walk_id == walk_id,
            WalkEmergencyCall.created_at >= now - RATE_LIMIT_WINDOW,
        )
        .order_by(WalkEmergencyCall.created_at.desc())
        .all()
    )
    if len(recent) >= RATE_LIMIT_MAX_CALLS:
        last = recent[0]
        LOGGER.warning("walk_emergency: rate-limit walk_id=%s acionamentos=%s", walk_id, len(recent))
        return _response(
            call_id=last.id, walk_id=walk_id, mode=last.mode, status=last.status,
            tutor_e164=_tutor_e164(db, tutor_id) if last.mode == "direct" else None,
            support_phone=support_phone, vet_name=vet_name, vet_phone=vet_phone,
            created_at=last.created_at, rate_limited=True, persisted=True,
        )

    plan = _plan_call(db, walk_id, tutor_id, walker_user_id)
    call_id = str(uuid4())
    persisted = True
    try:
        db.add(WalkEmergencyCall(
            id=call_id, tenant_id=tenant_id, walk_id=walk_id, walker_user_id=walker_user_id,
            tutor_user_id=tutor_id, reason=reason, mode=plan.mode, provider=plan.provider,
            provider_call_id=plan.provider_call_id, status=plan.status,
            fallback_reason=plan.fallback_reason, created_at=now,
        ))
        create_operational_event(
            db, walk, EMERGENCY_CALL, severity="high",
            notes=_event_notes(reason, plan.mode), dedupe=False,
        )
        db.commit()
    except Exception:  # noqa: BLE001 — a resposta com o número sai mesmo sem registro
        db.rollback()
        persisted = False
        LOGGER.exception("walk_emergency: falha ao gravar acionamento walk_id=%s", walk_id)

    if persisted:
        try:
            _notify_tutor(
                db, walk_id=walk_id, tutor_id=tutor_id, tenant_id=tenant_id,
                pet_name=pet_name, reason=reason, call_id=call_id,
            )
            _notify_admins(
                db, walk_id=walk_id, tenant_id=tenant_id,
                pet_name=pet_name, reason=reason, call_id=call_id,
            )
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            LOGGER.exception("walk_emergency: falha ao notificar walk_id=%s", walk_id)

    LOGGER.info(
        "walk_emergency: acionado walk_id=%s mode=%s status=%s persisted=%s",
        walk_id, plan.mode, plan.status, persisted,
    )
    return _response(
        call_id=call_id if persisted else None, walk_id=walk_id, mode=plan.mode, status=plan.status,
        tutor_e164=plan.tutor_e164, support_phone=support_phone, vet_name=vet_name,
        vet_phone=vet_phone, created_at=now, rate_limited=False, persisted=persisted,
    )
