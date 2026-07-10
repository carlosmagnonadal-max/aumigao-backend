from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.operational_beta_log import OperationalBetaLog
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_location_ping import WalkLocationPing
from app.services.operational_matching_service import (
    AUTO_REMATCHING,
    PENDING_ATTEMPT,
    PENDING_WALKER_CONFIRMATION,
    notify_walker_walk_event,
    process_expired_attempts,
)
from app.services.operational_observability_service import (
    record_operational_exception,
    record_operational_log,
)
from app.services.operational_reliability_service import (
    detect_reliability_events,
    record_operational_recovery,
)
from app.services.push_notifications import send_push_for_notification

SCHEDULER_STATE: dict[str, Any] = {
    "scheduler_running": False,
    "last_scheduler_cycle_at": None,
    "last_scheduler_error": None,
    "tasks_executed": {},
}
_CYCLE_LOCK = asyncio.Lock()

ACTIVE_NO_SHOW_STATUSES = {
    "walker_accepted",
    "ride_scheduled",
    "walker_arriving",
    "Indo buscar o pet",
    "Agendado",
}
RECOVERY_SIGNAL_STATUSES = {
    "no_walker_found",
    "walker_declined",
    "extended_matching",
    "auto_rematching",
}


def scheduler_interval_seconds() -> int:
    try:
        value = int(os.getenv("OPERATIONAL_SCHEDULER_INTERVAL_SECONDS", "30"))
        return max(10, value)
    except (TypeError, ValueError):
        return 30


def _utcnow() -> datetime:
    return datetime.utcnow()


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _walk_start(db: Session, walk: Walk) -> datetime | None:
    """INÍCIO do passeio em UTC naive. scheduled_date é hora LOCAL do tenant —
    ver app.lib.walk_time (bug 08/07: local tratado como UTC cancelava em 1 min)."""
    from app.lib.walk_time import tenant_tz_name, walk_start_utc

    return walk_start_utc(walk.scheduled_date, tenant_tz_name(db, walk.tenant_id))


def _context_json(log: OperationalBetaLog) -> dict:
    try:
        parsed = json.loads(log.context_json or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _recent_log_exists(
    db: Session,
    event_type: str,
    source: str,
    entity_id: str | None = None,
    within_minutes: int = 30,
) -> bool:
    cutoff = _utcnow() - timedelta(minutes=within_minutes)
    rows = (
        db.query(OperationalBetaLog)
        .filter(
            OperationalBetaLog.event_type == event_type,
            OperationalBetaLog.source == source,
            OperationalBetaLog.created_at >= cutoff,
        )
        .order_by(OperationalBetaLog.created_at.desc())
        .limit(20)
        .all()
    )
    if not entity_id:
        return bool(rows)
    return any(str(_context_json(row).get("walk_id") or _context_json(row).get("notification_id") or "") == str(entity_id) for row in rows)


def _run_task(session_factory, name: str, task) -> int:
    db = session_factory()
    # Fase 2c: scheduler roda tarefas de plataforma (cross-tenant) → acesso irrestrito.
    db.info["rls_tenant"] = "*"
    try:
        result = int(task(db) or 0)
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        record_operational_exception(
            db,
            event_type="scheduler_task_failed",
            source=f"scheduler.{name}",
            exc=exc,
            severity="error",
            context={"task": name},
        )
        try:
            db.commit()
        except Exception:
            db.rollback()
        SCHEDULER_STATE["last_scheduler_error"] = f"{name}: {exc}"
        return 0
    finally:
        db.close()


def _task_matching_expiration(db: Session) -> int:
    return process_expired_attempts(db, commit=False)


# Camada 2 do som (10/07): a tentativa de matching PENDENTE toca UM push
# (walker_attempt_created/new_walk) na criação; se o passeador não ouvir, silêncio
# até expirar. Reenvia o MESMO push crítico (canal walk-requests, whitelisted) em
# cadência a cada 5min, até 3 lembretes por tentativa.
WALK_REQUEST_REMINDER_INTERVAL_MINUTES = 5
WALK_REQUEST_REMINDER_MAX_COUNT = 3


def _walk_when_label(walk: Walk) -> str:
    """'de DD/MM às HH:MM' a partir de scheduled_date (hora de parede LOCAL do
    tenant — NÃO precisa converter timezone aqui, é só rótulo de exibição)."""
    from app.lib.walk_time import parse_wall_time

    parsed = parse_wall_time(walk.scheduled_date)
    if not parsed:
        return "agendado"
    return f"de {parsed.strftime('%d/%m')} às {parsed.strftime('%H:%M')}"


def _walk_request_reminders_sent(db: Session, attempt_id: str, within_minutes: int = 40) -> int:
    """Conta lembretes já enviados para esta tentativa (dedupe SEM migration nova:
    reusa OperationalBetaLog, mesmo padrão de _recent_log_exists, mas contando —
    não só checando existência — para respeitar o teto de 3)."""
    cutoff = _utcnow() - timedelta(minutes=within_minutes)
    rows = (
        db.query(OperationalBetaLog)
        .filter(
            OperationalBetaLog.event_type == "walk_request_reminder_sent",
            OperationalBetaLog.source == "scheduler.walk_reminders",
            OperationalBetaLog.created_at >= cutoff,
        )
        .all()
    )
    return sum(1 for row in rows if str(_context_json(row).get("attempt_id") or "") == str(attempt_id))


def _task_walk_request_reminders(db: Session) -> int:
    now = _utcnow()
    due_cutoff = now - timedelta(minutes=WALK_REQUEST_REMINDER_INTERVAL_MINUTES)
    attempts = (
        db.query(WalkMatchingAttempt)
        .filter(
            WalkMatchingAttempt.status == PENDING_ATTEMPT,
            WalkMatchingAttempt.expires_at > now,
            WalkMatchingAttempt.sent_at <= due_cutoff,
        )
        .order_by(WalkMatchingAttempt.sent_at.asc())
        .limit(100)
        .all()
    )
    count = 0
    for attempt in attempts:
        walk = db.get(Walk, attempt.walk_id)
        # Mesmo guard de process_expired_attempts: só re-alerta enquanto o walk
        # segue de fato em matching (nunca aceito/cancelado/reagendado).
        if not walk or walk.operational_status not in {PENDING_WALKER_CONFIRMATION, AUTO_REMATCHING}:
            continue

        reminders_sent = _walk_request_reminders_sent(db, attempt.id)
        if reminders_sent >= WALK_REQUEST_REMINDER_MAX_COUNT:
            continue
        reminder_number = reminders_sent + 1
        due_at = attempt.sent_at + timedelta(minutes=WALK_REQUEST_REMINDER_INTERVAL_MINUTES * reminder_number)
        if now < due_at:
            continue

        notify_walker_walk_event(
            db,
            walk,
            attempt.walker_id,
            title="Solicitação aguardando resposta",
            message=f"Solicitação aguardando: passeio {_walk_when_label(walk)} ainda precisa de resposta.",
            # Reusa o mesmo tipo whitelisted do push original (canal walk-requests,
            # som crítico) — ver push_notifications.WALK_REQUEST_NOTIFICATION_TYPES.
            notification_type="new_walk",
            priority="high",
            action="walker_attempt_created",
            metadata={
                "attempt_number": attempt.attempt_number,
                "reminder_number": reminder_number,
                "expires_at": attempt.expires_at,
            },
        )
        # Persiste a notificação/push ANTES do log-guard. record_operational_log
        # chama inspect() que, sob SQLite+StaticPool, provoca rollback implícito
        # da conexão — sem este commit o push poderia ser descartado por um log
        # best-effort (mesmo gotcha documentado em _task_expire_unpaid_walks).
        db.commit()
        record_operational_log(
            db,
            event_type="walk_request_reminder_sent",
            severity="info",
            source="scheduler.walk_reminders",
            message=f"Lembrete {reminder_number}/{WALK_REQUEST_REMINDER_MAX_COUNT} de solicitação pendente enviado.",
            context={
                "attempt_id": attempt.id,
                "walk_id": walk.id,
                "walker_id": attempt.walker_id,
                "reminder_number": reminder_number,
            },
        )
        count += 1
    return count


def _task_recovery_signals(db: Session) -> int:
    walks = (
        db.query(Walk)
        .filter(Walk.operational_status.in_(list(RECOVERY_SIGNAL_STATUSES)))
        .order_by(Walk.created_at.desc())
        .limit(50)
        .all()
    )
    count = 0
    for walk in walks:
        if _recent_log_exists(db, "operational_recovery_triggered", "scheduler.recovery", walk.id, within_minutes=60):
            continue
        record_operational_recovery(walk, db)
        record_operational_log(
            db,
            event_type="operational_recovery_triggered",
            severity="warning",
            source="scheduler.recovery",
            message="Scheduler identificou passeio aguardando recuperação operacional.",
            context={"walk_id": walk.id, "status": walk.operational_status},
        )
        count += 1
    return count


def _task_no_show_checkin(db: Session) -> int:
    now = _utcnow()
    grace_minutes = _int_env("OPERATIONAL_MISSING_CHECKIN_MINUTES", 45)
    walks = (
        db.query(Walk)
        .filter(Walk.operational_status.in_(list(ACTIVE_NO_SHOW_STATUSES)))
        .order_by(Walk.created_at.desc())
        .limit(100)
        .all()
    )
    count = 0
    for walk in walks:
        scheduled_at = _walk_start(db, walk)
        if scheduled_at and now < scheduled_at + timedelta(minutes=grace_minutes):
            continue
        count += len(detect_reliability_events(walk, db))
    return count


def _task_stuck_completions(db: Session) -> int:
    now = _utcnow()
    awaiting_minutes = _int_env("OPERATIONAL_AWAITING_COMPLETION_REVIEW_MINUTES", 180)
    ride_progress_extra_minutes = _int_env("OPERATIONAL_RIDE_IN_PROGRESS_EXTRA_MINUTES", 120)
    count = 0

    reviews = (
        db.query(WalkCompletionReview)
        .filter(WalkCompletionReview.status == "pending_review")
        .order_by(WalkCompletionReview.created_at.asc())
        .limit(100)
        .all()
    )
    for review in reviews:
        if not review.created_at or now < review.created_at + timedelta(minutes=awaiting_minutes):
            continue
        if _recent_log_exists(db, "completion_review_stuck", "scheduler.completion", review.walk_id, within_minutes=60):
            continue
        record_operational_log(
            db,
            event_type="completion_review_stuck",
            severity="warning",
            source="scheduler.completion",
            message="Finalização aguardando revisão operacional acima da janela esperada.",
            context={"walk_id": review.walk_id, "review_id": review.id, "created_at": review.created_at.isoformat()},
        )
        count += 1

    walks = (
        db.query(Walk)
        .filter(Walk.operational_status == "ride_in_progress")
        .order_by(Walk.created_at.desc())
        .limit(100)
        .all()
    )
    for walk in walks:
        scheduled_at = _walk_start(db, walk)
        duration = int(walk.duration_minutes or 0)
        reference = scheduled_at or walk.created_at
        if not reference or now < reference + timedelta(minutes=duration + ride_progress_extra_minutes):
            continue
        if _recent_log_exists(db, "ride_in_progress_stuck", "scheduler.completion", walk.id, within_minutes=60):
            continue
        record_operational_log(
            db,
            event_type="ride_in_progress_stuck",
            severity="warning",
            source="scheduler.completion",
            message="Passeio em andamento acima da janela operacional esperada.",
            context={"walk_id": walk.id, "scheduled_date": walk.scheduled_date, "duration_minutes": walk.duration_minutes},
        )
        count += 1
    return count


def _task_push_retry(db: Session) -> int:
    cutoff = _utcnow() - timedelta(minutes=_int_env("OPERATIONAL_PUSH_RETRY_LOOKBACK_MINUTES", 15))
    failures = (
        db.query(OperationalBetaLog)
        .filter(
            OperationalBetaLog.event_type == "push_failed",
            OperationalBetaLog.created_at >= cutoff,
        )
        .order_by(OperationalBetaLog.created_at.desc())
        .limit(10)
        .all()
    )
    count = 0
    for failure in failures:
        notification_id = _context_json(failure).get("notification_id")
        if not notification_id:
            continue
        if _recent_log_exists(db, "push_retry_attempted", "scheduler.push", str(notification_id), within_minutes=15):
            continue
        notification = db.get(Notification, str(notification_id))
        if not notification:
            continue
        send_push_for_notification(db, notification)
        record_operational_log(
            db,
            event_type="push_retry_attempted",
            severity="info",
            source="scheduler.push",
            message="Retry leve de push operacional executado.",
            context={"notification_id": notification.id, "type": notification.type},
        )
        count += 1
    return count


def _record_skipped_cycle(session_factory) -> None:
    db = session_factory()
    # Fase 2c: log de plataforma → acesso irrestrito.
    db.info["rls_tenant"] = "*"
    try:
        record_operational_log(
            db,
            event_type="scheduler_cycle_skipped",
            severity="info",
            source="scheduler.lock",
            message="Ciclo operacional pulado porque outro ciclo ainda estava em execução.",
            context={},
        )
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# Chave estável do advisory lock do Postgres que garante UM único scheduler
# rodando o ciclo entre todos os workers/réplicas (o _CYCLE_LOCK asyncio só
# coordena dentro de um processo). Valor arbitrário, fixo p/ o scheduler operacional.
_SCHEDULER_ADVISORY_LOCK_KEY = 905_712_001


def _is_postgres(db: Session) -> bool:
    bind = db.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


def _try_acquire_cross_process_lock(db: Session) -> bool | None:
    """Tenta o advisory lock do Postgres (1 scheduler entre workers).
    Retorna True/False no Postgres; None quando o backend não suporta (ex.: sqlite em teste)."""
    if not _is_postgres(db):
        return None
    from sqlalchemy import text

    return bool(
        db.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _SCHEDULER_ADVISORY_LOCK_KEY},
        ).scalar()
    )


def _release_cross_process_lock(db: Session) -> None:
    if not _is_postgres(db):
        return
    from sqlalchemy import text

    db.execute(
        text("SELECT pg_advisory_unlock(:k)"),
        {"k": _SCHEDULER_ADVISORY_LOCK_KEY},
    )


async def run_operational_scheduler_cycle(session_factory) -> dict:
    if _CYCLE_LOCK.locked():
        SCHEDULER_STATE["scheduler_running"] = True
        SCHEDULER_STATE["tasks_executed"] = {"cycle_skipped": 1}
        SCHEDULER_STATE["last_scheduler_error"] = None
        _record_skipped_cycle(session_factory)
        return get_operational_scheduler_status()

    async with _CYCLE_LOCK:
        await asyncio.sleep(0)
        # Trava entre processos: com vários workers, só quem pegar o advisory lock
        # roda o ciclo; os demais pulam (evita push/sinais duplicados). Em sqlite
        # (testes) acquired=None → roda normalmente, pois é processo único.
        lock_db = session_factory()
        # Fase 2c: lock de plataforma (cross-tenant) → acesso irrestrito.
        lock_db.info["rls_tenant"] = "*"
        acquired = _try_acquire_cross_process_lock(lock_db)
        if acquired is False:
            lock_db.close()
            SCHEDULER_STATE["scheduler_running"] = True
            SCHEDULER_STATE["tasks_executed"] = {"cycle_skipped_other_worker": 1}
            SCHEDULER_STATE["last_scheduler_error"] = None
            _record_skipped_cycle(session_factory)
            return get_operational_scheduler_status()
        try:
            return _run_operational_scheduler_cycle_locked(session_factory)
        finally:
            if acquired is True:
                _release_cross_process_lock(lock_db)
            lock_db.close()


def _task_purge_location_pings(db: Session) -> int:
    """Limpa pings de GPS antigos (retenção configurável, default 7 dias) — evita a
    tabela walk_location_pings crescer sem limite. Index em recorded_at torna o DELETE barato."""
    retention_days = _int_env("LOCATION_PINGS_RETENTION_DAYS", 7)
    cutoff = _utcnow() - timedelta(days=retention_days)
    deleted = (
        db.query(WalkLocationPing)
        .filter(WalkLocationPing.recorded_at < cutoff)
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def _task_pet_reminder_alerts(db: Session) -> int:
    """Delega para pet_reminder_service (import lazy para evitar ciclo).
    Gated por PET_ALERTS_ENABLED; guard diário de 23h interno ao serviço."""
    from app.services.pet_reminder_service import task_pet_reminder_alerts
    return task_pet_reminder_alerts(db)


def _cancel_pending_charge_for_walk(db: Session, walk_id: str) -> None:
    """Cancela no Asaas a cobrança PENDENTE do walk (best-effort) e marca o Payment
    local como `cancelado_regenerado`. Reusa o helper de mutação de cobrança (item B).
    Nunca levanta — dinheiro/estado do walk já foi decidido pelo caller.
    """
    try:
        from app.models.payment import Payment
        from app.routes.payments import PAYMENT_PENDING_STATUSES, cancel_asaas_charge_sync
        pendings = (
            db.query(Payment)
            .filter(
                Payment.walk_id == walk_id,
                Payment.status.in_(list(PAYMENT_PENDING_STATUSES)),
            )
            .all()
        )
        for pay in pendings:
            cancel_asaas_charge_sync(pay.provider, pay.provider_payment_id)
            pay.status = "cancelado_regenerado"
            db.add(pay)
    except Exception:
        # best-effort: cancelar a cobrança nunca deve travar a expiração do walk.
        pass


def _task_expire_unpaid_walks(db: Session) -> int:
    """R7 + corte de 45min: cancela passeios 'awaiting_payment' não pagos a tempo.

    Passeio nasce aguardando pagamento (gate REQUIRE_PAYMENT_BEFORE_MATCHING).
    Dois critérios de expiração (basta um):
      1. Timeout absoluto: criado há mais de WALK_PAYMENT_TIMEOUT_HOURS (default 24).
      2. Corte operacional: o INÍCIO do passeio está a menos de
         WALK_PAYMENT_CUTOFF_MINUTES (default 45) de agora, ou já passou — sem
         pagamento, não dá mais tempo de executar.
    Ao expirar (por qualquer critério): cancela TAMBÉM a cobrança pendente no Asaas
    (DELETE, best-effort). Idempotente; lote limitado a 50 por ciclo.
    """
    from app.services.operational_matching_service import notify_tutor_walk_event

    timeout_hours = _int_env("WALK_PAYMENT_TIMEOUT_HOURS", 24)
    cutoff_minutes = _int_env("WALK_PAYMENT_CUTOFF_MINUTES", 45)
    now = _utcnow()
    timeout_cutoff = now - timedelta(hours=timeout_hours)
    start_deadline = now + timedelta(minutes=cutoff_minutes)

    walks = (
        db.query(Walk)
        .filter(Walk.operational_status == "awaiting_payment")
        .order_by(Walk.created_at.asc())
        .limit(50)
        .all()
    )
    cancelled: list[tuple[str, str | None]] = []
    for walk in walks:
        timed_out = walk.created_at is not None and walk.created_at < timeout_cutoff
        # scheduled_date é hora LOCAL do tenant → converter pra UTC antes de comparar
        # (sem isso, 10:30 locais viram "10:30 UTC" e o corte dispara 3h mais cedo).
        walk_start = _walk_start(db, walk)
        past_cutoff = walk_start is not None and walk_start <= start_deadline
        if not (timed_out or past_cutoff):
            continue

        reason = (
            "Pagamento não confirmado a tempo (corte de %d min antes do início)." % cutoff_minutes
            if past_cutoff
            else "Pagamento não confirmado no prazo."
        )
        expiry_kind = "payment_cutoff" if past_cutoff else "payment_timeout"
        walk.operational_status = "ride_cancelled"
        walk.status = "Cancelado"
        walk.no_walker_reason = reason
        db.add(walk)
        # Cancela a cobrança pendente no Asaas antes de seguir (best-effort).
        _cancel_pending_charge_for_walk(db, walk.id)
        try:
            notify_tutor_walk_event(
                db,
                walk,
                title="Passeio cancelado",
                message="Seu passeio foi cancelado porque o pagamento não foi confirmado a tempo.",
                notification_type="walk_status",
                priority="high",
                action="ride_cancelled",
                metadata={"reason": expiry_kind},
            )
        except Exception:
            # Notificação é best-effort: nunca impede o cancelamento (estado > push).
            pass
        cancelled.append((walk.id, walk.tenant_id))

    # Persiste o cancelamento (dinheiro/estado) ANTES da observabilidade. record_operational_log
    # chama inspect() que, sob SQLite+StaticPool, provoca rollback implícito da conexão — o
    # commit aqui garante que o cancelamento NUNCA seja descartado por um log best-effort.
    if cancelled:
        db.commit()
    for walk_id, tenant_id in cancelled:
        record_operational_log(
            db,
            event_type="unpaid_walk_expired",
            severity="info",
            source="scheduler.payment_expiry",
            message="Passeio cancelado por pagamento não confirmado a tempo.",
            context={
                "walk_id": walk_id,
                "tenant_id": tenant_id,
                "timeout_hours": timeout_hours,
                "cutoff_minutes": cutoff_minutes,
            },
        )
    return len(cancelled)


def _task_cost_alerts(db: Session) -> int:
    """Avalia alertas de custo. Guard de 15 min — orçamento não precisa de
    cadência de 60s e a avaliação varre commission_entries por tenant."""
    if _recent_log_exists(db, "cost_alerts_evaluated", "scheduler.cost_alerts", within_minutes=15):
        return 0
    from app.services.cost_alert_service import evaluate_cost_alerts

    fired = evaluate_cost_alerts(db)
    record_operational_log(
        db,
        event_type="cost_alerts_evaluated",
        severity="info",
        source="scheduler.cost_alerts",
        message="Alertas de custo avaliados.",
        context={"fired": fired},
    )
    return fired


def _run_operational_scheduler_cycle_locked(session_factory) -> dict:
    SCHEDULER_STATE["scheduler_running"] = True
    SCHEDULER_STATE["last_scheduler_cycle_at"] = _utcnow().isoformat()
    SCHEDULER_STATE["last_scheduler_error"] = None

    tasks = {
        "matching_expiration": _task_matching_expiration,
        # Camada 2 do som (10/07): roda LOGO DEPOIS de matching_expiration, no
        # mesmo ciclo, para nunca re-alertar uma tentativa que acabou de expirar.
        "walk_request_reminders": _task_walk_request_reminders,
        "recovery_signals": _task_recovery_signals,
        "no_show_checkin": _task_no_show_checkin,
        "stuck_completions": _task_stuck_completions,
        "push_retry": _task_push_retry,
        "purge_location_pings": _task_purge_location_pings,
        # Fase 3 Perfil Vivo: lembretes determinísticos (vacina/aniversário/inatividade).
        # Gated por PET_ALERTS_ENABLED (default off). Guard diário de 23h interno.
        "pet_reminder_alerts": _task_pet_reminder_alerts,
        # R7: cancela passeios não pagos após WALK_PAYMENT_TIMEOUT_HOURS (default 24h).
        "expire_unpaid_walks": _task_expire_unpaid_walks,
        # Alertas de custo (10/07): orçamento do tenant × comissão medida.
        "cost_alerts": _task_cost_alerts,
    }
    results = {name: _run_task(session_factory, name, task) for name, task in tasks.items()}
    SCHEDULER_STATE["tasks_executed"] = results
    return get_operational_scheduler_status()


def mark_operational_scheduler_stopped(error: str | None = None) -> None:
    SCHEDULER_STATE["scheduler_running"] = False
    if error:
        SCHEDULER_STATE["last_scheduler_error"] = error


def mark_operational_scheduler_started() -> None:
    SCHEDULER_STATE["scheduler_running"] = True


def get_operational_scheduler_status() -> dict:
    return {
        "scheduler_running": bool(SCHEDULER_STATE.get("scheduler_running")),
        "last_scheduler_cycle_at": SCHEDULER_STATE.get("last_scheduler_cycle_at"),
        "last_scheduler_error": SCHEDULER_STATE.get("last_scheduler_error"),
        "tasks_executed": dict(SCHEDULER_STATE.get("tasks_executed") or {}),
        "interval_seconds": scheduler_interval_seconds(),
    }
