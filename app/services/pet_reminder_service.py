"""pet_reminder_service.py — Task de alertas determinísticos do Perfil Vivo do Pet (Fase 3).

Lógica de varredura cross-tenant diária: vacina/vermífugo, aniversário, inatividade.
Separado do operational_scheduler_service para manter cada arquivo < 500 linhas.
O scheduler apenas delega: `_task_pet_reminder_alerts = task_pet_reminder_alerts`.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_reminder import PetReminder
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.constants import WALK_COMPLETED_STATUSES
from app.services.operational_observability_service import record_operational_log

# Guard diário: não re-rodar se já rodou nas últimas 23h (1380 minutos).
_SWEEP_GUARD_MINUTES = 1380

# Idempotência de notificação por reminder: não reenvia antes de 7 dias (vacina/vermífugo).
_VACCINE_COOLDOWN_DAYS = 7


def _env_on(name: str) -> bool:
    return os.getenv(name, "false").lower() in {"1", "true", "yes", "on"}


def _utcnow() -> datetime:
    return datetime.utcnow()


def _today() -> date:
    return _utcnow().date()


def _recent_log_exists_reminders(db: Session, within_minutes: int) -> bool:
    """Verifica se há log de sweep recente (guard diário)."""
    from app.models.operational_beta_log import OperationalBetaLog

    cutoff = _utcnow() - timedelta(minutes=within_minutes)
    return bool(
        db.query(OperationalBetaLog)
        .filter(
            OperationalBetaLog.event_type == "pet_reminder_sweep",
            OperationalBetaLog.source == "scheduler.reminders",
            OperationalBetaLog.created_at >= cutoff,
        )
        .first()
    )


def _create_reminder_notification(db: Session, pet: Pet, reminder: PetReminder) -> None:
    """Dispara notificação push para o tutor do pet (padrão tutor_referral_notify.py)."""
    from app.routes.notifications import NotificationCreate, _create_notification

    kind = reminder.kind
    if kind == "vaccine":
        title = f"💉 Vacina do {pet.name} vencendo"
        message = f"A vacina do {pet.name} está se aproximando do vencimento. Lembre-se de agendar com o veterinário!"
    elif kind == "vermifuge":
        title = f"💊 Vermífugo do {pet.name} vencendo"
        message = f"Está na hora de administrar o vermífugo do {pet.name}. Consulte seu veterinário."
    elif kind == "birthday":
        title = f"🎂 Hoje é aniversário do {pet.name}!"
        message = f"Parabéns ao {pet.name}! Comemore esse dia especial com muito carinho e passeios."
    elif kind == "inactivity":
        title = f"🐕 {pet.name} está sentindo falta dos passeios"
        message = f"Faz um tempo que o {pet.name} não passeia. Que tal agendar um passeio hoje?"
    else:
        title = f"Lembrete para {pet.name}"
        message = "Há um lembrete pendente para o seu pet."

    _create_notification(db, NotificationCreate(
        tenant_id=pet.tenant_id,
        user_id=pet.tutor_id,
        user_role="tutor",
        title=title,
        message=message,
        type="pet_reminder",
        related_entity_type="pet_reminder",
        related_entity_id=reminder.id,
        metadata={"kind": kind, "pet_id": pet.id},
    ))


def _upsert_reminder(db: Session, pet: Pet, kind: str, due: date,
                     source_event_id: str | None = None) -> PetReminder:
    """Upsert: busca reminder ativo por (pet_id, kind) — para birthday/inactivity —
    ou por (pet_id, kind, source_event_id) para vaccine/vermifuge.
    Atualiza due_date se mudou; cria se não existe."""
    q = db.query(PetReminder).filter(
        PetReminder.pet_id == pet.id,
        PetReminder.kind == kind,
        PetReminder.active == True,  # noqa: E712
    )
    if source_event_id:
        q = q.filter(PetReminder.source_event_id == source_event_id)
    existing = q.first()
    if existing:
        if existing.due_date != due:
            existing.due_date = due
        db.flush()
        return existing
    reminder = PetReminder(
        id=str(uuid4()),
        pet_id=pet.id,
        tenant_id=pet.tenant_id,
        kind=kind,
        due_date=due,
        active=True,
        source_event_id=source_event_id,
        created_at=_utcnow(),
    )
    db.add(reminder)
    db.flush()
    return reminder


def _process_tenant(db: Session, tenant: Tenant) -> int:
    """Processa alertas para um tenant com reminders_active. Retorna count notificado."""
    from app.services.pet_profile_service import reminders_active, get_or_create_pet_profile_config

    if not reminders_active(tenant, db):
        return 0

    cfg: PetProfileConfig = get_or_create_pet_profile_config(db, tenant.id)
    vaccine_lead_days: int = cfg.vaccine_lead_days
    inactivity_days: int = cfg.inactivity_days

    today = _today()
    now = _utcnow()
    count = 0

    # -----------------------------------------------------------------------
    # 1. Vacina / vermífugo
    # -----------------------------------------------------------------------
    # Janela: due_date <= hoje + vaccine_lead_days
    window_end = today + timedelta(days=vaccine_lead_days)
    cooldown_cutoff = now - timedelta(days=_VACCINE_COOLDOWN_DAYS)

    vaccine_reminders = (
        db.query(PetReminder)
        .filter(
            PetReminder.tenant_id == tenant.id,
            PetReminder.kind.in_(["vaccine", "vermifuge"]),
            PetReminder.active == True,  # noqa: E712
            PetReminder.due_date <= window_end,
        )
        .all()
    )
    for reminder in vaccine_reminders:
        # Idempotência: não notifica se já enviou nos últimos 7 dias
        if reminder.last_notified_at and reminder.last_notified_at >= cooldown_cutoff:
            continue
        pet = db.get(Pet, reminder.pet_id)
        if not pet:
            continue
        _create_reminder_notification(db, pet, reminder)
        reminder.last_notified_at = now
        db.flush()
        count += 1

    # -----------------------------------------------------------------------
    # 2. Aniversário
    # -----------------------------------------------------------------------
    pets_with_birthday = (
        db.query(Pet)
        .filter(
            Pet.tenant_id == tenant.id,
            Pet.birth_date.isnot(None),
        )
        .all()
    )
    for pet in pets_with_birthday:
        if pet.birth_date is None:
            continue
        if pet.birth_date.month != today.month or pet.birth_date.day != today.day:
            continue
        # Upsert do reminder de aniversário (1 por pet/kind)
        reminder = _upsert_reminder(db, pet, "birthday", today)
        # Notifica apenas se não enviou este ano
        if reminder.last_notified_at and reminder.last_notified_at.year == today.year:
            continue
        _create_reminder_notification(db, pet, reminder)
        reminder.last_notified_at = now
        db.flush()
        count += 1

    # -----------------------------------------------------------------------
    # 3. Inatividade
    # -----------------------------------------------------------------------
    # Pets que TÊM pelo menos um passeio concluído e cujo último está vencido.
    inactivity_cutoff_dt = now - timedelta(days=inactivity_days)
    inactivity_cooldown_cutoff = now - timedelta(days=inactivity_days)

    all_pets = (
        db.query(Pet)
        .filter(Pet.tenant_id == tenant.id)
        .all()
    )
    for pet in all_pets:
        # Busca o último passeio concluído deste pet
        last_walk = (
            db.query(Walk)
            .filter(
                Walk.pet_id == pet.id,
                Walk.status.in_(list(WALK_COMPLETED_STATUSES)),
            )
            .order_by(Walk.created_at.desc())
            .first()
        )
        if not last_walk:
            # Nunca passeou → não alerta (evita spam em pet recém-cadastrado)
            continue
        # Se o último passeio concluído é recente → não alerta
        if last_walk.created_at >= inactivity_cutoff_dt:
            continue
        # Upsert do reminder de inatividade
        reminder = _upsert_reminder(db, pet, "inactivity", today)
        # Idempotência: não reenvia antes de inactivity_days
        if reminder.last_notified_at and reminder.last_notified_at >= inactivity_cooldown_cutoff:
            continue
        _create_reminder_notification(db, pet, reminder)
        reminder.last_notified_at = now
        db.flush()
        count += 1

    return count


def task_pet_reminder_alerts(db: Session) -> int:
    """Entry-point chamado pelo scheduler. Retorna número de notificações enviadas.

    Curto-circuito se PET_ALERTS_ENABLED=off (dormência total).
    Guard diário de 23h para evitar re-runs no mesmo dia.
    """
    # Curto-circuito barato: feature global desligada
    if not _env_on("PET_ALERTS_ENABLED"):
        return 0

    # Guard diário (1380 minutos = 23h)
    if _recent_log_exists_reminders(db, within_minutes=_SWEEP_GUARD_MINUTES):
        return 0

    total = 0
    tenants = db.query(Tenant).all()
    for tenant in tenants:
        total += _process_tenant(db, tenant)

    # Persiste notificações e atualizações de last_notified_at antes de chamar
    # record_operational_log. Essa função usa inspect(engine).has_table() internamente,
    # que no SQLite+StaticPool (ambiente de teste) pode causar rollback implícito da
    # conexão se houver transação pendente. Em produção (PostgreSQL) não há problema,
    # mas o commit explícito aqui garante correção em ambos os ambientes.
    db.flush()
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    record_operational_log(
        db,
        event_type="pet_reminder_sweep",
        severity="info",
        source="scheduler.reminders",
        message=f"Sweep de lembretes do pet concluído: {total} notificações enviadas.",
        context={"notified": total},
    )
    return total
