from __future__ import annotations

import json
import logging
import re as _re
from datetime import datetime, timedelta
from uuid import uuid4

_SAFE_IDENT_RE = _re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _safe_ident(name: str) -> str:
    """Validate a SQL identifier before DDL interpolation. Raises ValueError if unsafe."""
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"Unsafe SQL identifier rejected: {name!r}")
    return name

from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt, WalkOperationalLog
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_location_ping import WalkLocationPing
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.walk_review import WalkReview
from app.models.walk_tip import WalkTip
from app.models.walker_profile import WalkerProfile
from app.schemas.matching import MatchingWalkerRequest
from app.services.matching_service import get_eligible_walkers, matched_walker_payload, parse_datetime
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.services.walker_availability_service import _covers
from app.services.operational_observability_service import record_operational_exception, record_operational_log
from app.services.operational_reliability_service import serialize_operational_event
from app.services.walker_operational_score_service import calculate_walker_operational_score
from app.services.walker_network_matching_service import get_matching_pool_for_tenant, is_walker_eligible_for_tenant
from app.services.reputation_service import reputation_summary as _reputation_summary
from app.routes.notifications import NotificationCreate, _create_notification

logger = logging.getLogger(__name__)

PENDING_WALKER_CONFIRMATION = "pending_walker_confirmation"
WALKER_ACCEPTED = "walker_accepted"
WALKER_DECLINED = "walker_declined"
AUTO_REMATCHING = "auto_rematching"
NO_WALKER_FOUND = "no_walker_found"
RIDE_SCHEDULED = "ride_scheduled"
WALKER_ARRIVING = "walker_arriving"
RIDE_IN_PROGRESS = "ride_in_progress"
RIDE_COMPLETED = "ride_completed"
RIDE_CANCELLED = "ride_cancelled"
AWAITING_TUTOR_RECONFIRMATION = "awaiting_tutor_reconfirmation"

LEGACY_STATUS_TO_OPERATIONAL = {
    "Agendado": RIDE_SCHEDULED,
    "Indo buscar o pet": WALKER_ARRIVING,
    "Passeando agora": RIDE_IN_PROGRESS,
    "Finalizado": RIDE_COMPLETED,
    "Cancelado": RIDE_CANCELLED,
}
OPERATIONAL_TO_LEGACY_STATUS = {
    PENDING_WALKER_CONFIRMATION: "Agendado",
    WALKER_ACCEPTED: "Agendado",
    WALKER_DECLINED: "Agendado",
    AUTO_REMATCHING: "Agendado",
    NO_WALKER_FOUND: "Agendado",
    RIDE_SCHEDULED: "Agendado",
    WALKER_ARRIVING: "Indo buscar o pet",
    RIDE_IN_PROGRESS: "Passeando agora",
    RIDE_COMPLETED: "Finalizado",
    RIDE_CANCELLED: "Cancelado",
}

PENDING_ATTEMPT = "pending"
ACCEPTED_ATTEMPT = "accepted"
DECLINED_ATTEMPT = "declined"
EXPIRED_ATTEMPT = "expired"
SKIPPED_ATTEMPT = "skipped"

MAX_ATTEMPTS = 3
CONFIRMATION_TIMEOUT = timedelta(minutes=30)

def notify_tutor_walk_event(
    db: Session,
    walk: Walk,
    title: str,
    message: str,
    notification_type: str = "walk_status",
    priority: str = "medium",
    action: str | None = None,
    metadata: dict | None = None,
) -> None:
    if not walk.tutor_id:
        return

    _create_notification(
        db,
        NotificationCreate(
            user_id=walk.tutor_id,
            user_role="tutor",
            title=title,
            message=message,
            type=notification_type,
            related_entity_type="walk",
            related_entity_id=walk.id,
            metadata={
                "priority": priority,
                "channel": "in_app",
                "action": action or walk.operational_status,
                **(metadata or {}),
            },
        ),
    )

def utcnow() -> datetime:
    return datetime.utcnow()


def ensure_operational_schema(engine) -> None:
    datetime_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
    columns = {
        "operational_status": "VARCHAR DEFAULT 'ride_scheduled'",
        "walker_selection_mode": "VARCHAR DEFAULT 'auto'",
        "assigned_walker_id": "VARCHAR",
        "current_attempt": "INTEGER DEFAULT 0",
        "max_attempts": "INTEGER DEFAULT 3",
        "confirmation_expires_at": datetime_type,
        "matching_started_at": datetime_type,
        "matching_finished_at": datetime_type,
        "no_walker_reason": "TEXT",
    }
    inspector = inspect(engine)
    if "walks" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("walks")}
    with engine.begin() as conn:
        for name, definition in columns.items():
            if name not in existing:
                safe_name = _safe_ident(name)  # column names are hardcoded dict keys
                # nosec: definition is a SQL type string from the hardcoded columns dict
                conn.execute(text(f"ALTER TABLE walks ADD COLUMN {safe_name} {definition}"))


def _json_load(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _json_dump(value: dict | list | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _response_time(sent_at: datetime | None, responded_at: datetime | None) -> int | None:
    if not sent_at or not responded_at:
        return None
    return max(0, int((responded_at - sent_at).total_seconds()))


def _walk_neighborhood(walk: Walk) -> str:
    raw = (walk.address_snapshot or "").replace("\n", " ")
    pieces = [piece.strip() for piece in raw.split("-") if piece.strip()]
    if len(pieces) >= 2:
        return pieces[-1]
    comma_pieces = [piece.strip() for piece in raw.split(",") if piece.strip()]
    if len(comma_pieces) >= 3:
        return comma_pieces[-1]
    return raw or "Regiao de retirada"


def coarse_pickup_payload(walk: Walk) -> dict:
    region = _walk_neighborhood(walk)
    return {
        "pickup_region_label": region,
        "pickup_distance_label": "Distancia aproximada em calculo",
        "address_snapshot": "",
        "notes": "",
    }


def should_release_address(walk: Walk, user: User | None) -> bool:
    if not user:
        return False
    if user.role in {"admin", "super_admin"} or walk.tutor_id == user.id:
        return True
    return (
        user.role == "walker"
        and walk.walker_id == user.id
        and walk.operational_status in {WALKER_ACCEPTED, RIDE_SCHEDULED, WALKER_ARRIVING, RIDE_IN_PROGRESS, RIDE_COMPLETED}
    )


def log_event(
    db: Session,
    walk_id: str,
    event_type: str,
    actor_type: str = "system",
    actor_id: str | None = None,
    metadata: dict | None = None,
) -> WalkOperationalLog:
    row = WalkOperationalLog(
        id=str(uuid4()),
        walk_id=walk_id,
        actor_type=actor_type,
        actor_id=actor_id,
        event_type=event_type,
        metadata_json=_json_dump(metadata or {}),
    )
    db.add(row)
    return row


def serialize_attempt(attempt: WalkMatchingAttempt) -> dict:
    return {
        "id": attempt.id,
        "walk_id": attempt.walk_id,
        "walker_id": attempt.walker_id,
        "attempt_number": attempt.attempt_number,
        "status": attempt.status,
        "score": attempt.score,
        "score_breakdown": _json_load(attempt.score_breakdown, {}),
        "sent_at": attempt.sent_at,
        "responded_at": attempt.responded_at,
        "expires_at": attempt.expires_at,
        "response_time_seconds": attempt.response_time_seconds,
        "reason": attempt.reason,
        "created_at": attempt.created_at,
        "updated_at": attempt.updated_at,
    }


def serialize_log(log: WalkOperationalLog) -> dict:
    return {
        "id": log.id,
        "walk_id": log.walk_id,
        "actor_type": log.actor_type,
        "actor_id": log.actor_id,
        "event_type": log.event_type,
        "metadata": _json_load(log.metadata_json, {}),
        "created_at": log.created_at,
    }


def _serialize_completion_review(review: WalkCompletionReview | None) -> dict | None:
    if not review:
        return None

    return {
        "id": review.id,
        "status": review.status,
        "photo_url": review.photo_url,
        "notes": review.notes,
        "checklist": _json_load(review.checklist_json, {}),
        "admin_note": review.admin_note,
        "reviewed_at": review.reviewed_at,
        "created_at": review.created_at,
    }


def _serialize_walk_review(review: WalkReview | None) -> dict | None:
    if not review:
        return None

    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "tutor_id": review.tutor_id,
        "walker_id": review.walker_id,
        "rating": review.rating,
        "comment": review.comment,
        "tags": _json_load(review.tags_json, []),
        "created_at": review.created_at,
    }


def _serialize_walk_tip(tip: WalkTip | None) -> dict | None:
    if not tip:
        return None

    return {
        "id": tip.id,
        "walk_id": tip.walk_id,
        "tutor_id": tip.tutor_id,
        "walker_id": tip.walker_id,
        "amount": tip.amount,
        "status": tip.status,
        "provider": tip.provider,
        "checkout_url": tip.checkout_url,
        "created_at": tip.created_at,
        "paid_at": tip.paid_at,
    }


def _has_live_tracking(walk_id: str, db: Session) -> bool:
    """Retorna True se existir ping de localização nos últimos 2 minutos.

    Usa .first() is not None em vez de .limit(1).count() para evitar COUNT(*) desnecessário.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=2)
    return (
        db.query(WalkLocationPing)
        .filter(WalkLocationPing.walk_id == walk_id, WalkLocationPing.recorded_at >= cutoff)
        .first()
        is not None
    )


def _batch_live_tracking(walk_ids: list[str], db: Session) -> set[str]:
    """Retorna o conjunto de walk_ids com ping de localização nos últimos 2 minutos.

    1 query IN para toda a listagem — elimina o N+1 de _has_live_tracking em listagens.
    """
    if not walk_ids:
        return set()
    cutoff = datetime.utcnow() - timedelta(minutes=2)
    rows = (
        db.query(WalkLocationPing.walk_id)
        .filter(WalkLocationPing.walk_id.in_(walk_ids), WalkLocationPing.recorded_at >= cutoff)
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def serialize_operational_walk(
    walk: Walk,
    db: Session,
    user: User | None = None,
    include_private: bool = False,
    live_tracking_ids: set[str] | None = None,
) -> dict:
    """Serializa um Walk com todos os campos operacionais.

    live_tracking_ids: conjunto pré-computado por _batch_live_tracking (listagens).
    Se None, calcula individualmente (detalhe único — 1 query OK).
    O campo `has_live_tracking` está sempre presente no JSON para compatibilidade
    com clients em produção.
    """
    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    tutor = db.get(User, walk.tutor_id) if walk.tutor_id else None
    tenant = db.get(Tenant, walk.tenant_id) if walk.tenant_id else None
    walker_id = walk.walker_id or walk.assigned_walker_id
    walker = db.get(User, walker_id) if walker_id else None
    walker_profile = (
      db.query(WalkerProfile)
      .filter(WalkerProfile.user_id == walker_id)
      .first()
      if walker_id
      else None
    )
    walker_photo_url = (walker_profile.profile_photo_url if walker_profile else "") or ""
    attempts = (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk.id)
        .order_by(WalkMatchingAttempt.attempt_number.asc())
        .all()
    )
    logs = (
        db.query(WalkOperationalLog)
        .filter(WalkOperationalLog.walk_id == walk.id)
        .order_by(WalkOperationalLog.created_at.asc())
        .all()
    )
    operational_events = (
        db.query(WalkOperationalEvent)
        .filter(WalkOperationalEvent.walk_id == walk.id)
        .order_by(WalkOperationalEvent.created_at.desc())
        .all()
    )
    completion_review = (
        db.query(WalkCompletionReview)
        .filter(WalkCompletionReview.walk_id == walk.id)
        .order_by(WalkCompletionReview.created_at.desc())
        .first()
    )
    walk_review = (
        db.query(WalkReview)
        .filter(WalkReview.walk_id == walk.id)
        .order_by(WalkReview.created_at.desc())
        .first()
    )
    paid_tip = (
        db.query(WalkTip)
        .filter(WalkTip.walk_id == walk.id, WalkTip.status == "paid")
        .order_by(WalkTip.paid_at.desc(), WalkTip.created_at.desc())
        .first()
    )
    latest_tip = (
        db.query(WalkTip)
        .filter(WalkTip.walk_id == walk.id)
        .order_by(WalkTip.created_at.desc())
        .first()
    )
    visible_tip = paid_tip or latest_tip
    walker_operational_score = calculate_walker_operational_score(walker_id, db) if walker_id else None
    # BUG 1 fix: rating médio do walker atribuído ao passeio, reutilizando reputation_summary
    _walker_rep = _reputation_summary(walker_id, db) if walker_id else None
    walker_rating_avg = _walker_rep["rating_average"] if _walker_rep and _walker_rep["reviews_count"] > 0 else None
    walk_date, _, walk_time = (walk.scheduled_date or "").partition("T")
    can_see_full = include_private or should_release_address(walk, user)
    address_payload = {"address_snapshot": walk.address_snapshot, "notes": walk.notes} if can_see_full else coarse_pickup_payload(walk)
    pet_photo_url = (pet.photo_url if pet else "") or ""
    if pet_photo_url.startswith(("file://", "content://", "blob:")):
        pet_photo_url = ""
    return {
        "id": walk.id,
        "tutor_id": walk.tutor_id,
        "walker_id": walker_id,
        "assigned_walker_id": walk.assigned_walker_id,
        "assignedWalkerId": walk.assigned_walker_id,
        "pet_id": walk.pet_id,
        "pet_name": pet.name if pet else None,
        "pet_photo_url": pet_photo_url,
        "tutor_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "client_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "walker_name": (walker.full_name if walker else None) or (walker.email if walker else None),
        "walker_photo_url": walker_photo_url,
        "profile_photo_url": walker_photo_url,
        "photo_url": walker_photo_url,
        "walker_operational_score": walker_operational_score,
        "walker_rating_avg": walker_rating_avg,
        "scheduled_date": walk.scheduled_date,
        "walk_date": walk_date or None,
        "walk_time": (walk_time[:5] if walk_time else None),
        "duration_minutes": walk.duration_minutes,
        "price": walk.price,
        "status": walk.status,
        "operational_status": walk.operational_status,
        "operationalStatus": walk.operational_status,
        "walker_selection_mode": walk.walker_selection_mode or "auto",
        "walkerSelectionMode": walk.walker_selection_mode or "auto",
        "pickup_method": walk.pickup_method,
        **address_payload,
        "pickup_privacy_level": "full" if can_see_full else "coarse",
        "current_attempt": walk.current_attempt,
        "current_matching_attempt": walk.current_attempt,
        "max_attempts": walk.max_attempts,
        "max_matching_attempts": walk.max_attempts,
        "confirmation_expires_at": walk.confirmation_expires_at,
        "walker_confirmation_expires_at": walk.confirmation_expires_at,
        "matching_started_at": walk.matching_started_at,
        "matching_finished_at": walk.matching_finished_at,
        "no_walker_reason": walk.no_walker_reason,
        "matching_attempts": [serialize_attempt(item) for item in attempts],
        "operational_logs": [serialize_log(item) for item in logs],
        "operational_events": [serialize_operational_event(item) for item in operational_events],
        "completion_review": _serialize_completion_review(completion_review),
        "review": _serialize_walk_review(walk_review),
        "tip": _serialize_walk_tip(visible_tip),
        "tip_id": visible_tip.id if visible_tip else None,
        "tip_amount": visible_tip.amount if visible_tip and visible_tip.status == "paid" else 0,
        "tip_status": visible_tip.status if visible_tip else None,
        "tip_paid_at": visible_tip.paid_at if visible_tip else None,
        "created_at": walk.created_at,
        # has_live_tracking: se live_tracking_ids foi pré-computado (batch de listagem)
        # usa lookup em set (O(1)); senão faz 1 query — OK para endpoints de detalhe.
        "has_live_tracking": (
            walk.id in live_tracking_ids
            if live_tracking_ids is not None
            else _has_live_tracking(walk.id, db)
        ),
        # ── Fase 1 Passo 3: informação de tenant por passeio ──────────────────
        "tenant_id": walk.tenant_id,
        "tenant_name": (
            tenant.branding.display_name
            if tenant and tenant.branding and tenant.branding.display_name
            else (tenant.name if tenant else None)
        ),
        "tenant_brand_color": (tenant.branding.primary_color if tenant and tenant.branding else None),
    }


def _current_pending_attempt(db: Session, walk_id: str) -> WalkMatchingAttempt | None:
    return (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk_id, WalkMatchingAttempt.status == PENDING_ATTEMPT)
        .order_by(WalkMatchingAttempt.attempt_number.desc())
        .first()
    )


def _candidate_request(walk: Walk) -> MatchingWalkerRequest:
    return MatchingWalkerRequest(
        pet_id=walk.pet_id,
        scheduled_at=walk.scheduled_date,
        duration_minutes=walk.duration_minutes,
        pickup_method=walk.pickup_method,
        modality=getattr(walk, "modality", "standard") or "standard",
        neighborhood=_walk_neighborhood(walk),
        tenant_id=getattr(walk, "tenant_id", None),
    )


def _tenant_matching_pool(walk: Walk, db: Session) -> set[str] | None:
    tenant_id = getattr(walk, "tenant_id", None)
    if not tenant_id:
        return None
    try:
        walker_ids = get_matching_pool_for_tenant(db, tenant_id)
    except Exception as exc:
        logger.warning(
            "walker_network_matching_pool_unavailable walk_id=%s tenant_id=%s error=%s",
            walk.id,
            tenant_id,
            exc,
        )
        return set()
    return set(walker_ids)


def _rank_candidates(walk: Walk, db: Session, excluded: set[str]) -> list[dict]:
    tenant_pool = _tenant_matching_pool(walk, db)
    profiles = [
        profile
        for profile in get_eligible_walkers(_candidate_request(walk), db)
        if profile.user_id not in excluded
        and (tenant_pool is None or profile.user_id in tenant_pool)
    ]
    items = [matched_walker_payload(profile, _candidate_request(walk), db) for profile in profiles]
    items.sort(
        key=lambda item: (
            item.get("final_matching_score", 0),
            item.get("rating_average", 0),
            item.get("proximity_score", 0),
            item.get("total_walks", 0),
        ),
        reverse=True,
    )
    return items


def _candidate_for_selected_walker(walk: Walk, walker_id: str, db: Session) -> dict | None:
    tenant_id = getattr(walk, "tenant_id", None)
    if tenant_id and not is_walker_eligible_for_tenant(db, tenant_id, walker_id):
        return None

    profile = db.query(WalkerProfile).filter(
        WalkerProfile.user_id == walker_id,
        WalkerProfile.status == "active",
        WalkerProfile.active_as_walker.is_(True),
    ).first()
    if profile:
        return matched_walker_payload(profile, _candidate_request(walk), db)
    user = db.get(User, walker_id)
    if user and user.role == "walker" and user.is_active:
        return {
            "walker_id": user.id,
            "name": user.full_name or user.email,
            "final_matching_score": 70,
            "proximity_score": 60,
            "rating_score": 75,
            "experience_score": 55,
            "availability_score": 80,
            "distance_km": None,
            "level": "Prata",
        }
    return None


def _create_attempt(db: Session, walk: Walk, candidate: dict, attempt_number: int) -> WalkMatchingAttempt:
    now = utcnow()
    expires_at = now + CONFIRMATION_TIMEOUT
    attempt = WalkMatchingAttempt(
        id=str(uuid4()),
        walk_id=walk.id,
        walker_id=str(candidate["walker_id"]),
        attempt_number=attempt_number,
        status=PENDING_ATTEMPT,
        score=float(candidate.get("final_matching_score") or candidate.get("matching_score_base") or 0),
        score_breakdown=_json_dump({
            "distance": candidate.get("proximity_score"),
            "acceptance_rate": candidate.get("acceptance_rate_score", 75),
            "cancellations": candidate.get("cancellation_score", 90),
            "average_response": candidate.get("average_response_score", 80),
            "rating": candidate.get("rating_score"),
            "experience_score": candidate.get("experience_score"),
            "behavior_score": candidate.get("behavior_score"),
            "premium": candidate.get("boost_score", 0),
            "level": candidate.get("level"),
        }),
        sent_at=now,
        expires_at=expires_at,
    )
    db.add(attempt)
    walk.assigned_walker_id = attempt.walker_id
    walk.walker_id = attempt.walker_id
    walk.current_attempt = attempt_number
    walk.max_attempts = walk.max_attempts or MAX_ATTEMPTS
    walk.confirmation_expires_at = expires_at
    walk.operational_status = PENDING_WALKER_CONFIRMATION if attempt_number == 1 or (walk.walker_selection_mode or "auto") == "only_selected" else AUTO_REMATCHING
    walk.status = OPERATIONAL_TO_LEGACY_STATUS[walk.operational_status]
    log_event(db, walk.id, "walker_attempt_created", metadata={"walker_id": attempt.walker_id, "attempt_number": attempt_number, "score": attempt.score})
    db.flush()

    notify_walker_walk_event(
        db,
        walk,
        attempt.walker_id,
        title="Novo passeio disponível",
        message="Você recebeu uma solicitação de passeio. Aceite ou recuse dentro do prazo para manter sua pontuação operacional.",
        notification_type="new_walk",
        priority="high",
        action="walker_attempt_created",
        metadata={
            "attempt_number": attempt_number,
            "expires_at": expires_at,
            "score": attempt.score,
        },
    )

    if attempt_number == 1:
        notify_tutor_walk_event(
            db,
            walk,
            title="Passeador em confirmação",
            message="Encontramos um passeador disponível e estamos aguardando a confirmação dele.",
            notification_type="walker_attempt_created",
            priority="medium",
            action=walk.operational_status,
            metadata={"attempt_number": attempt_number, "walker_id": attempt.walker_id},
        )
    return attempt


def _selected_walker_unavailable(walk: Walk, db: Session, reason: str) -> Walk:
    walk.operational_status = AWAITING_TUTOR_RECONFIRMATION
    walk.status = "Aguardando confirmação do tutor"
    walk.no_walker_reason = reason
    walk.confirmation_expires_at = None
    walk.matching_finished_at = utcnow()
    log_event(
        db,
        walk.id,
        "selected_walker_unavailable",
        metadata={
            "reason": reason,
            "walker_selection_mode": walk.walker_selection_mode or "auto",
            "walker_id": walk.assigned_walker_id or walk.walker_id,
        },
    )
    notify_tutor_walk_event(
        db,
        walk,
        title="Passeador escolhido indisponível",
        message=reason,
        notification_type="walk_recovery",
        priority="high",
        action=AWAITING_TUTOR_RECONFIRMATION,
        metadata={
            "reason": reason,
            "walker_selection_mode": walk.walker_selection_mode or "auto",
            "walker_id": walk.assigned_walker_id or walk.walker_id,
        },
    )
    return walk


def start_matching(walk: Walk, db: Session, actor: User | None = None) -> Walk:
    process_expired_attempts(db, commit=False)
    if walk.operational_status in {WALKER_ACCEPTED, RIDE_SCHEDULED, WALKER_ARRIVING, RIDE_IN_PROGRESS, RIDE_COMPLETED} and walk.matching_finished_at:
        return walk
    pending = _current_pending_attempt(db, walk.id)
    if pending:
        return walk

    walk.max_attempts = walk.max_attempts or MAX_ATTEMPTS
    walk.matching_started_at = walk.matching_started_at or utcnow()
    walk.matching_finished_at = None
    walk.no_walker_reason = None
    log_event(db, walk.id, "matching_started", actor_type=(actor.role if actor else "system"), actor_id=(actor.id if actor else None))

    notify_tutor_walk_event(
        db,
        walk,
        title="Buscando passeador",
        message="Estamos procurando o melhor passeador disponível para o passeio do seu pet.",
        notification_type="matching_started",
        priority="medium",
        action="matching_started",
    )

    selected_id = walk.assigned_walker_id or walk.walker_id
    if (walk.walker_selection_mode or "auto") == "only_selected":
        candidate = _candidate_for_selected_walker(walk, selected_id, db) if selected_id else None
        if not candidate:
            return _selected_walker_unavailable(
                walk,
                db,
                "Passeador escolhido indisponível. Aguardando decisão do tutor.",
            )
        attempt_number = (
            db.query(WalkMatchingAttempt)
            .filter(WalkMatchingAttempt.walk_id == walk.id)
            .count()
            + 1
        )
        _create_attempt(db, walk, candidate, attempt_number)
        return walk

    excluded: set[str] = set()
    candidate = _candidate_for_selected_walker(walk, selected_id, db) if selected_id else None
    if not candidate:
        ranked = _rank_candidates(walk, db, excluded)
        candidate = ranked[0] if ranked else None

    if not candidate:
        walk.operational_status = NO_WALKER_FOUND
        walk.status = OPERATIONAL_TO_LEGACY_STATUS[NO_WALKER_FOUND]
        walk.no_walker_reason = "Nenhum passeador elegivel encontrado."
        walk.matching_finished_at = utcnow()
        log_event(db, walk.id, "no_walker_found", metadata={"reason": walk.no_walker_reason})
        record_operational_log(
            db,
            event_type="matching_failed",
            severity="warning",
            source="matching.start",
            message="Nenhum passeador elegível encontrado para o passeio.",
            context={"walk_id": walk.id, "reason": walk.no_walker_reason},
        )
        return walk

    _create_attempt(db, walk, candidate, 1)
    return walk


def _finish_attempt(attempt: WalkMatchingAttempt, status: str, reason: str | None = None) -> None:
    now = utcnow()
    attempt.status = status
    attempt.responded_at = now
    attempt.response_time_seconds = _response_time(attempt.sent_at, now)
    attempt.reason = reason
    attempt.updated_at = now


def rematch(walk: Walk, db: Session, reason: str = "rematch") -> Walk:
    attempts = (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk.id)
        .order_by(WalkMatchingAttempt.attempt_number.asc())
        .all()
    )
    next_number = len(attempts) + 1
    if (walk.walker_selection_mode or "auto") == "only_selected":
        return _selected_walker_unavailable(
            walk,
            db,
            "Passeador escolhido não confirmou. Aguardando decisão do tutor.",
        )

    if next_number > (walk.max_attempts or MAX_ATTEMPTS):
        walk.operational_status = AWAITING_TUTOR_RECONFIRMATION
        walk.status = "Aguardando confirmação do tutor"
        walk.no_walker_reason = "Limite de tentativas atingido. Aguardando confirmação do tutor para continuar a busca."
        walk.confirmation_expires_at = None
        log_event(
            db,
            walk.id,
            "awaiting_tutor_reconfirmation",
            metadata={
                "reason": walk.no_walker_reason,
                "attempts": len(attempts),
                "max_attempts": walk.max_attempts or MAX_ATTEMPTS,
            }
        )
        record_operational_log(
            db,
            event_type="operational_recovery_triggered",
            severity="warning",
            source="matching.rematch",
            message="Recovery operacional acionado após limite de tentativas.",
            context={"walk_id": walk.id, "attempts": len(attempts), "max_attempts": walk.max_attempts or MAX_ATTEMPTS},
        )
        notify_tutor_walk_event(
            db,
            walk,
            title="Confirme seu passeio",
            message="Não conseguimos confirmar um passeador nas tentativas iniciais. Você pode continuar a busca, reagendar ou cancelar sem custo.",
            notification_type="walk_recovery",
            priority="high",
            action=AWAITING_TUTOR_RECONFIRMATION,
            metadata={"attempts": len(attempts), "max_attempts": walk.max_attempts or MAX_ATTEMPTS},
        )
        return walk

    excluded = {attempt.walker_id for attempt in attempts}
    ranked = _rank_candidates(walk, db, excluded)
    candidate = ranked[0] if ranked else None
    if not candidate:
        walk.operational_status = NO_WALKER_FOUND
        walk.status = OPERATIONAL_TO_LEGACY_STATUS[NO_WALKER_FOUND]
        walk.no_walker_reason = "Sem passeadores elegiveis para rematch."
        walk.matching_finished_at = utcnow()
        walk.confirmation_expires_at = None
        log_event(db, walk.id, "no_walker_found", metadata={"reason": walk.no_walker_reason})
        record_operational_log(
            db,
            event_type="matching_failed",
            severity="warning",
            source="matching.rematch",
            message="Rematch sem passeadores elegíveis.",
            context={"walk_id": walk.id, "reason": walk.no_walker_reason, "attempts": len(attempts)},
        )
        notify_tutor_walk_event(
            db,
            walk,
            title="Nenhum passeador encontrado",
            message="Ainda não encontramos um passeador disponível para este horário. Nossa equipe pode orientar os próximos passos.",
            notification_type="no_walker_found",
            priority="high",
            action=NO_WALKER_FOUND,
        )
        return walk

    walk.operational_status = AUTO_REMATCHING
    walk.status = OPERATIONAL_TO_LEGACY_STATUS[AUTO_REMATCHING]
    log_event(db, walk.id, "rematch_started", metadata={"reason": reason, "attempt_number": next_number})
    _create_attempt(db, walk, candidate, next_number)
    notify_tutor_walk_event(
        db,
        walk,
        title="Buscando substituto",
        message="Estamos buscando outro passeador disponível para manter seu passeio.",
        notification_type="auto_rematching",
        priority="high",
        action=AUTO_REMATCHING,
        metadata={"reason": reason, "attempt_number": next_number},
    )
    return walk

def decline_walk(walk: Walk, walker: User, db: Session) -> Walk:
    process_expired_attempts(db, commit=False)
    attempt = _current_pending_attempt(db, walk.id)
    if not attempt or attempt.walker_id != walker.id:
        raise HTTPException(status_code=403, detail="Solicitacao nao atribuida a este passeador.")

    _finish_attempt(attempt, DECLINED_ATTEMPT, "walker_declined")
    walk.operational_status = WALKER_DECLINED
    walk.status = OPERATIONAL_TO_LEGACY_STATUS[WALKER_DECLINED]

    log_event(
        db,
        walk.id,
        "walker_declined",
        actor_type="walker",
        actor_id=walker.id,
        metadata={"attempt_number": attempt.attempt_number},
    )

    notify_tutor_walk_event(
        db,
        walk,
        title="Passeador indisponível",
        message="O passeador não pôde atender este passeio. Já estamos buscando uma alternativa.",
        notification_type="walker_declined",
        priority="high",
        action=WALKER_DECLINED,
        metadata={"attempt_number": attempt.attempt_number},
    )

    return rematch(walk, db, reason="walker_declined")


def accept_walk(walk: Walk, walker: User, db: Session) -> Walk:
    process_expired_attempts(db, commit=False)

    if walk.operational_status in {WALKER_ACCEPTED, RIDE_SCHEDULED} and walk.walker_id == walker.id:
        return walk

    attempt = _current_pending_attempt(db, walk.id)

    if not attempt or attempt.walker_id != walker.id:
        raise HTTPException(status_code=403, detail="Solicitacao nao atribuida a este passeador.")

    if attempt.expires_at <= utcnow():
        _finish_attempt(attempt, EXPIRED_ATTEMPT, "expired_before_accept")
        log_event(
            db,
            walk.id,
            "walker_expired",
            actor_type="system",
            metadata={"walker_id": walker.id, "attempt_number": attempt.attempt_number},
        )
        rematch(walk, db, reason="expired")
        raise HTTPException(status_code=409, detail="Tempo de aceite expirado.")

    tenant_id = getattr(walk, "tenant_id", None)
    if tenant_id and not is_walker_eligible_for_tenant(db, tenant_id, walker.id):
        raise HTTPException(status_code=403, detail="Passeador nao elegivel para este tenant.")

    _walk_dt = parse_datetime(walk.scheduled_date)
    if _walk_dt is not None:
        _blocks = (
            db.query(WalkerAvailabilityException)
            .filter(
                WalkerAvailabilityException.walker_user_id == walker.id,
                WalkerAvailabilityException.exception_date == _walk_dt.date(),
                WalkerAvailabilityException.kind == "block",
            )
            .all()
        )
        if any(_covers(b, _walk_dt.strftime("%H:%M")) for b in _blocks):
            raise HTTPException(status_code=409, detail="Passeador bloqueou disponibilidade nesta data/horario.")

    updated = (
        db.query(Walk)
        .filter(
            Walk.id == walk.id,
            Walk.assigned_walker_id == walker.id,
            Walk.operational_status.in_([PENDING_WALKER_CONFIRMATION, AUTO_REMATCHING]),
        )
        .update(
            {
                Walk.walker_id: walker.id,
                Walk.operational_status: WALKER_ACCEPTED,
                Walk.status: OPERATIONAL_TO_LEGACY_STATUS[WALKER_ACCEPTED],
                Walk.matching_finished_at: utcnow(),
                Walk.confirmation_expires_at: None,
            },
            synchronize_session=False,
        )
    )

    if updated != 1:
        raise HTTPException(status_code=409, detail="Este passeio ja foi aceito ou atualizado.")

    db.flush()
    db.refresh(walk)

    _finish_attempt(attempt, ACCEPTED_ATTEMPT, "walker_accepted")

    log_event(
        db,
        walk.id,
        "walker_accepted",
        actor_type="walker",
        actor_id=walker.id,
        metadata={"attempt_number": attempt.attempt_number},
    )

    log_event(db, walk.id, "address_released", actor_type="system", actor_id=walker.id)

    notify_tutor_walk_event(
        db,
        walk,
        title="Passeador confirmado",
        message="O passeador aceitou o passeio do seu pet.",
        notification_type="walker_accepted",
        priority="medium",
        action=WALKER_ACCEPTED,
        metadata={"walker_id": walker.id, "attempt_number": attempt.attempt_number},
    )

    notify_walker_walk_event(
        db,
        walk,
        walker.id,
        title="Passeio confirmado",
        message="Você aceitou o passeio. O endereço do tutor já foi liberado conforme as regras operacionais.",
        notification_type="walker_accepted",
        priority="medium",
        action=WALKER_ACCEPTED,
        metadata={
            "attempt_number": attempt.attempt_number,
            "address_released": True,
        },
    )

    return walk

def process_expired_attempts(db: Session, commit: bool = True) -> int:
    """Expire pending attempts and persist the resulting rematch/recovery state.

    This function owns its commit because the background scheduler depends on it
    running outside a request-level transaction. It remains idempotent by only
    selecting attempts that are still pending.
    """
    try:
        now = utcnow()
        expired = (
            db.query(WalkMatchingAttempt)
            .filter(WalkMatchingAttempt.status == PENDING_ATTEMPT, WalkMatchingAttempt.expires_at <= now)
            .order_by(WalkMatchingAttempt.expires_at.asc())
            .all()
        )
        count = 0
        for attempt in expired:
            walk = db.get(Walk, attempt.walk_id)
            if not walk or walk.operational_status not in {PENDING_WALKER_CONFIRMATION, AUTO_REMATCHING}:
                continue
            _finish_attempt(attempt, EXPIRED_ATTEMPT, "confirmation_timeout")
            log_event(db, walk.id, "walker_expired", actor_type="system", metadata={"walker_id": attempt.walker_id, "attempt_number": attempt.attempt_number})
            record_operational_log(
                db,
                event_type="matching_timeout",
                severity="warning",
                source="matching.timeout",
                message="Tempo de aceite do passeador expirou.",
                context={"walk_id": walk.id, "walker_id": attempt.walker_id, "attempt_number": attempt.attempt_number},
            )

            notify_walker_walk_event(
                db,
                walk,
                attempt.walker_id,
            title="Tempo de aceite expirado",
            message="O prazo para aceitar este passeio expirou. A solicitação foi redirecionada para outro passeador.",
                notification_type="acceptance_expired",
                priority="medium",
                action="walker_expired",
                metadata={
                    "attempt_number": attempt.attempt_number,
                },
            )

            notify_tutor_walk_event(
                db,
                walk,
            title="Tempo de confirmação expirado",
            message="O passeador não respondeu dentro do prazo. Estamos buscando uma alternativa.",
                notification_type="walker_expired",
                priority="high",
                action="walker_expired",
                metadata={"walker_id": attempt.walker_id, "attempt_number": attempt.attempt_number},
            )

            rematch(walk, db, reason="expired")
            count += 1

        if count and commit:
            db.commit()
        return count
    except Exception as exc:
        if commit:
            db.rollback()
            try:
                record_operational_exception(
                    db,
                    event_type="matching_exception",
                    source="matching.process_expired_attempts",
                    exc=exc,
                    severity="error",
                )
                db.commit()
            except Exception:
                db.rollback()
        raise

def notify_walker_walk_event(
    db: Session,
    walk: Walk,
    walker_id: str | None,
    title: str,
    message: str,
    notification_type: str = "walker_walk_event",
    priority: str = "medium",
    action: str | None = None,
    metadata: dict | None = None,
) -> None:
    if not walker_id:
        return

    _create_notification(
        db,
        NotificationCreate(
            user_id=walker_id,
            user_role="walker",
            title=title,
            message=message,
            type=notification_type,
            related_entity_type="walk",
            related_entity_id=walk.id,
            metadata={
                "priority": priority,
                "channel": "in_app",
                "action": action or walk.operational_status,
                **(metadata or {}),
            },
        ),
    )

def update_operational_status(walk: Walk, status: str, db: Session, actor: User | None = None) -> Walk:
    walk.operational_status = LEGACY_STATUS_TO_OPERATIONAL.get(status, status)
    walk.status = OPERATIONAL_TO_LEGACY_STATUS.get(walk.operational_status, status)
    if walk.operational_status in {RIDE_COMPLETED, RIDE_CANCELLED}:
        walk.matching_finished_at = walk.matching_finished_at or utcnow()
    log_event(db, walk.id, "status_changed", actor_type=(actor.role if actor else "system"), actor_id=(actor.id if actor else None), metadata={"status": walk.status, "operational_status": walk.operational_status})
    return walk


def operational_metrics(db: Session) -> dict:
    attempts = db.query(WalkMatchingAttempt).all()
    logs = db.query(WalkOperationalLog).all()
    accepted = len([item for item in attempts if item.status == ACCEPTED_ATTEMPT])
    declined = len([item for item in attempts if item.status == DECLINED_ATTEMPT])
    expired = len([item for item in attempts if item.status == EXPIRED_ATTEMPT])
    response_times = [item.response_time_seconds for item in attempts if item.response_time_seconds is not None]
    total = max(1, len(attempts))
    avg_response = round(sum(response_times) / len(response_times)) if response_times else 0
    return {
        "acceptance_rate": round((accepted / total) * 100),
        "avg_response_seconds": avg_response,
        "declines": declined,
        "expirations": expired,
        "rematches": len([item for item in logs if item.event_type == "rematch_started"]),
        "cancellations": db.query(Walk).filter(Walk.operational_status == RIDE_CANCELLED).count(),
        "operational_score": max(0, min(100, 90 + accepted * 2 - declined - expired * 2)),
        "operational_efficiency": max(0, min(100, round((accepted / total) * 100) - expired * 2)),
    }
