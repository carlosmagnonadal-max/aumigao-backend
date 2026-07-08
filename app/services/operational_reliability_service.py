from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.walk import Walk
from app.models.walk_operational_event import WalkOperationalEvent

_logger = logging.getLogger("aumigao.operational_reliability")

WALKER_LATE = "walker_late"
TUTOR_UNREACHABLE = "tutor_unreachable"
WALKER_NO_SHOW = "walker_no_show"
TUTOR_NO_SHOW = "tutor_no_show"
LATE_CANCELLATION = "late_cancellation"
MISSING_CHECKIN = "missing_checkin"
OPERATIONAL_RECOVERY_TRIGGERED = "operational_recovery_triggered"

EVENT_LABELS = {
    WALKER_LATE: "Possível atraso do passeador",
    TUTOR_UNREACHABLE: "Tutor temporariamente indisponível",
    WALKER_NO_SHOW: "Possível no-show do passeador",
    TUTOR_NO_SHOW: "Possível no-show do tutor",
    LATE_CANCELLATION: "Cancelamento próximo ao horário",
    MISSING_CHECKIN: "Check-in operacional ausente",
    OPERATIONAL_RECOVERY_TRIGGERED: "Recovery operacional acionado",
}

SEVERITY_LABELS = {
    "low": "baixo",
    "medium": "moderado",
    "high": "alto",
}

ACTIVE_PRE_START_STATUSES = {
    "walker_accepted",
    "ride_scheduled",
    "walker_arriving",
    "Indo buscar o pet",
    "Agendado",
}

CANCELLED_STATUSES = {
    "ride_cancelled",
    "cancelled",
    "Cancelado",
    "canceled_by_tutor",
}


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _scheduled_start_utc(db: Session, walk: Walk) -> datetime | None:
    """INÍCIO do passeio em UTC naive (scheduled_date é hora LOCAL do tenant)."""
    from app.lib.walk_time import tenant_tz_name, walk_start_utc

    return walk_start_utc(walk.scheduled_date, tenant_tz_name(db, walk.tenant_id))


def _walk_status_key(walk: Walk) -> str:
    return str(walk.operational_status or walk.status or "").strip()


def _existing_event(db: Session, walk_id: str, event_type: str) -> WalkOperationalEvent | None:
    return (
        db.query(WalkOperationalEvent)
        .filter(WalkOperationalEvent.walk_id == walk_id, WalkOperationalEvent.event_type == event_type)
        .order_by(WalkOperationalEvent.created_at.desc())
        .first()
    )


def create_operational_event(
    db: Session,
    walk: Walk,
    event_type: str,
    severity: str = "low",
    notes: str | None = None,
    dedupe: bool = True,
) -> WalkOperationalEvent | None:
    if dedupe and _existing_event(db, walk.id, event_type):
        return None

    event = WalkOperationalEvent(
        id=str(uuid4()),
        walk_id=walk.id,
        walker_id=walk.walker_id or walk.assigned_walker_id,
        tutor_id=walk.tutor_id,
        event_type=event_type,
        severity=severity if severity in SEVERITY_LABELS else "low",
        notes=notes or EVENT_LABELS.get(event_type, "Evento operacional registrado."),
    )
    db.add(event)

    # ── Gancho E: penalidade CR por no-show do passeador ────────────────────
    # Dispara apenas quando WALKER_NO_SHOW é criado pela primeira vez
    # (dedupe acima já garante idempotência de evento; also_awarded garante
    # idempotência de CR caso o dedupe seja contornado).
    if event_type == WALKER_NO_SHOW:
        _walker_id = walk.walker_id or walk.assigned_walker_id
        if _walker_id:
            try:
                # Import tardio para evitar ciclo de importação.
                import app.services.walker_cr_service as _cr_svc
                from app.services.walker_cr_rules import CR_PENALTY

                if not _cr_svc.already_awarded(db, _walker_id, "no_show", walk.id):
                    _cr_svc.penalty_cr(
                        db,
                        _walker_id,
                        CR_PENALTY["no_show"],
                        "no_show",
                        description=f"No-show registrado para o passeio {walk.id}.",
                        related_entity_type="walk",
                        related_entity_id=walk.id,
                    )
            except Exception as _cr_exc:
                _logger.warning(
                    "Gancho CR no_show falhou (walk=%s): %s", walk.id, _cr_exc
                )

    return event


def detect_reliability_events(walk: Walk, db: Session) -> list[WalkOperationalEvent]:
    scheduled_at = _scheduled_start_utc(db, walk)
    if not scheduled_at:
        return []

    now = datetime.utcnow()
    status_key = _walk_status_key(walk)
    created: list[WalkOperationalEvent] = []

    late_minutes = _int_env("OPERATIONAL_WALKER_LATE_MINUTES", 20)
    missing_checkin_minutes = _int_env("OPERATIONAL_MISSING_CHECKIN_MINUTES", 45)

    if status_key == "walker_arriving" and now >= scheduled_at + timedelta(minutes=late_minutes):
        event = create_operational_event(
            db,
            walk,
            WALKER_LATE,
            "medium",
            "Acompanhando possível atraso operacional.",
        )
        if event:
            created.append(event)

    if status_key in ACTIVE_PRE_START_STATUSES and now >= scheduled_at + timedelta(minutes=missing_checkin_minutes):
        event = create_operational_event(
            db,
            walk,
            MISSING_CHECKIN,
            "high",
            "Equipe monitorando estabilidade do passeio por ausência de início operacional.",
        )
        if event:
            created.append(event)

    return created


def record_late_cancellation_if_applicable(walk: Walk, db: Session) -> WalkOperationalEvent | None:
    scheduled_at = _scheduled_start_utc(db, walk)
    if not scheduled_at or _walk_status_key(walk) not in CANCELLED_STATUSES:
        return None

    window_minutes = _int_env("OPERATIONAL_LATE_CANCELLATION_MINUTES", 60)
    if datetime.utcnow() < scheduled_at - timedelta(minutes=window_minutes):
        return None

    return create_operational_event(
        db,
        walk,
        LATE_CANCELLATION,
        "medium",
        "Cancelamento registrado próximo ao horário do passeio.",
    )


def record_operational_recovery(walk: Walk, db: Session) -> WalkOperationalEvent | None:
    return create_operational_event(
        db,
        walk,
        OPERATIONAL_RECOVERY_TRIGGERED,
        "high",
        "Recovery operacional acionado para preservar o acompanhamento do passeio.",
    )


def serialize_operational_event(event: WalkOperationalEvent) -> dict:
    return {
        "id": event.id,
        "walk_id": event.walk_id,
        "walker_id": event.walker_id,
        "tutor_id": event.tutor_id,
        "event_type": event.event_type,
        "label": EVENT_LABELS.get(event.event_type, event.event_type),
        "severity": event.severity,
        "severity_label": SEVERITY_LABELS.get(event.severity, event.severity),
        "notes": event.notes,
        "created_at": event.created_at,
    }
