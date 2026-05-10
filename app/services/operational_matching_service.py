from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk, WalkMatchingAttempt, WalkOperationalLog
from app.models.walker_profile import WalkerProfile
from app.schemas.matching import MatchingWalkerRequest
from app.services.matching_service import get_eligible_walkers, matched_walker_payload

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
CONFIRMATION_TIMEOUT = timedelta(minutes=5)


def utcnow() -> datetime:
    return datetime.utcnow()


def ensure_operational_schema(engine) -> None:
    datetime_type = "TIMESTAMP" if engine.dialect.name == "postgresql" else "DATETIME"
    columns = {
        "operational_status": "VARCHAR DEFAULT 'ride_scheduled'",
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
                conn.execute(text(f"ALTER TABLE walks ADD COLUMN {name} {definition}"))


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


def serialize_operational_walk(walk: Walk, db: Session, user: User | None = None, include_private: bool = False) -> dict:
    pet = db.get(Pet, walk.pet_id) if walk.pet_id else None
    tutor = db.get(User, walk.tutor_id) if walk.tutor_id else None
    walker_id = walk.walker_id or walk.assigned_walker_id
    walker = db.get(User, walker_id) if walker_id else None
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
    walk_date, _, walk_time = (walk.scheduled_date or "").partition("T")
    can_see_full = include_private or should_release_address(walk, user)
    address_payload = {"address_snapshot": walk.address_snapshot, "notes": walk.notes} if can_see_full else coarse_pickup_payload(walk)
    return {
        "id": walk.id,
        "tutor_id": walk.tutor_id,
        "walker_id": walker_id,
        "assigned_walker_id": walk.assigned_walker_id,
        "assignedWalkerId": walk.assigned_walker_id,
        "pet_id": walk.pet_id,
        "pet_name": pet.name if pet else None,
        "tutor_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "client_name": (tutor.full_name if tutor else None) or (tutor.email if tutor else None),
        "walker_name": (walker.full_name if walker else None) or (walker.email if walker else None),
        "scheduled_date": walk.scheduled_date,
        "walk_date": walk_date or None,
        "walk_time": (walk_time[:5] if walk_time else None),
        "duration_minutes": walk.duration_minutes,
        "price": walk.price,
        "status": walk.status,
        "operational_status": walk.operational_status,
        "operationalStatus": walk.operational_status,
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
        "created_at": walk.created_at,
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
        neighborhood=_walk_neighborhood(walk),
    )


def _rank_candidates(walk: Walk, db: Session, excluded: set[str]) -> list[dict]:
    profiles = [profile for profile in get_eligible_walkers(_candidate_request(walk), db) if profile.user_id not in excluded]
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
            "level": "Confiavel",
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
            "pet_size_experience": candidate.get("experience_score"),
            "pet_behavior_experience": candidate.get("behavior_score"),
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
    walk.operational_status = PENDING_WALKER_CONFIRMATION if attempt_number == 1 else AUTO_REMATCHING
    walk.status = OPERATIONAL_TO_LEGACY_STATUS[walk.operational_status]
    log_event(db, walk.id, "walker_attempt_created", metadata={"walker_id": attempt.walker_id, "attempt_number": attempt_number, "score": attempt.score})
    db.flush()
    return attempt


def start_matching(walk: Walk, db: Session, actor: User | None = None) -> Walk:
    process_expired_attempts(db)
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

    selected_id = walk.assigned_walker_id or walk.walker_id
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
    if next_number > (walk.max_attempts or MAX_ATTEMPTS):
        walk.operational_status = NO_WALKER_FOUND
        walk.status = OPERATIONAL_TO_LEGACY_STATUS[NO_WALKER_FOUND]
        walk.no_walker_reason = "Limite de tentativas atingido."
        walk.matching_finished_at = utcnow()
        walk.confirmation_expires_at = None
        log_event(db, walk.id, "no_walker_found", metadata={"reason": walk.no_walker_reason})
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
        return walk

    walk.operational_status = AUTO_REMATCHING
    walk.status = OPERATIONAL_TO_LEGACY_STATUS[AUTO_REMATCHING]
    log_event(db, walk.id, "rematch_started", metadata={"reason": reason, "attempt_number": next_number})
    _create_attempt(db, walk, candidate, next_number)
    return walk


def decline_walk(walk: Walk, walker: User, db: Session) -> Walk:
    process_expired_attempts(db)
    attempt = _current_pending_attempt(db, walk.id)
    if not attempt or attempt.walker_id != walker.id:
        raise HTTPException(status_code=403, detail="Solicitacao nao atribuida a este passeador.")
    _finish_attempt(attempt, DECLINED_ATTEMPT, "walker_declined")
    walk.operational_status = WALKER_DECLINED
    walk.status = OPERATIONAL_TO_LEGACY_STATUS[WALKER_DECLINED]
    log_event(db, walk.id, "walker_declined", actor_type="walker", actor_id=walker.id, metadata={"attempt_number": attempt.attempt_number})
    return rematch(walk, db, reason="walker_declined")


def accept_walk(walk: Walk, walker: User, db: Session) -> Walk:
    process_expired_attempts(db)
    if walk.operational_status in {WALKER_ACCEPTED, RIDE_SCHEDULED} and walk.walker_id == walker.id:
        return walk
    attempt = _current_pending_attempt(db, walk.id)
    if not attempt or attempt.walker_id != walker.id:
        raise HTTPException(status_code=403, detail="Solicitacao nao atribuida a este passeador.")
    if attempt.expires_at <= utcnow():
        _finish_attempt(attempt, EXPIRED_ATTEMPT, "expired_before_accept")
        log_event(db, walk.id, "walker_expired", actor_type="system", metadata={"walker_id": walker.id, "attempt_number": attempt.attempt_number})
        rematch(walk, db, reason="expired")
        raise HTTPException(status_code=409, detail="Tempo de aceite expirado.")

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
    log_event(db, walk.id, "walker_accepted", actor_type="walker", actor_id=walker.id, metadata={"attempt_number": attempt.attempt_number})
    log_event(db, walk.id, "address_released", actor_type="system", actor_id=walker.id)
    return walk


def process_expired_attempts(db: Session) -> int:
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
        rematch(walk, db, reason="expired")
        count += 1
    if count:
        db.commit()
    return count


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
