from uuid import uuid4
from datetime import datetime, timedelta
import json
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_review import WalkReview
from app.models.walk_tip import WalkTip
from app.models.user import User
from app.models.pet import Pet
from app.schemas.walk import WalkCreate, WalkResponse, WalkUpdateStatus
from app.schemas.walk_review import ALLOWED_WALK_REVIEW_TAGS, WalkReviewCreate
from app.schemas.walk_tip import WalkTipCheckoutCreate
from app.schemas.complaint import ComplaintCreate, ComplaintEvidenceCreate
from app.services.complaint_service import create_complaint
from app.services.operational_matching_service import (
    LEGACY_STATUS_TO_OPERATIONAL,
    RIDE_SCHEDULED,
    log_event,
    process_expired_attempts,
    serialize_operational_walk,
    start_matching,
    update_operational_status,
)
from app.services.operational_reliability_service import detect_reliability_events, record_late_cancellation_if_applicable

router = APIRouter(prefix="/walks", tags=["walks"])

COMPLETED_WALK_STATUSES = {"Finalizado", "Concluido", "Concluído", "finalizado", "completed", "finished"}
DIRECT_COMPLETION_STATUSES = {"ride_completed", "Finalizado", "finalizado", "completed", "finished"}
REVIEWABLE_COMPLETION_STATUSES = {"ride_completed"}
TIP_STATUSES = {"pending", "paid", "failed", "cancelled"}
TIP_PROVIDER = "internal_mock"


class WalkTipCreate(BaseModel):
    amount: float = Field(gt=0, le=500)
    note: str | None = None


class WalkReconfirmationDecision(BaseModel):
    action: str | None = None
    decision: str | None = None


FORBIDDEN_RESCHEDULE_FIELDS = {
    "price",
    "duration_minutes",
    "pet_id",
    "walker_id",
    "assigned_walker_id",
    "walker_selection_mode",
}


def _split_scheduled_date(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    date_part, _, time_part = value.partition("T")
    return date_part or None, time_part[:5] or None


def _parse_scheduled_at(value: str) -> datetime:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="Informe o novo horario do passeio.")
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="Horario do passeio invalido.")


def _serialize_walk(walk: Walk, db: Session) -> dict:
    return serialize_operational_walk(walk, db, include_private=True)


def _serialize_walk_review(review: WalkReview) -> dict:
    try:
        tags = json.loads(review.tags_json or "[]")
    except (TypeError, ValueError):
        tags = []
    return {
        "id": review.id,
        "walk_id": review.walk_id,
        "tutor_id": review.tutor_id,
        "walker_id": review.walker_id,
        "rating": review.rating,
        "comment": review.comment,
        "tags": tags if isinstance(tags, list) else [],
        "created_at": review.created_at,
    }


def _serialize_walk_tip(tip: WalkTip) -> dict:
    return {
        "id": tip.id,
        "tip_id": tip.id,
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


def _approved_completion_review_exists(walk_id: str, db: Session) -> bool:
    return (
        db.query(WalkCompletionReview)
        .filter(WalkCompletionReview.walk_id == walk_id, WalkCompletionReview.status == "approved")
        .first()
        is not None
    )


def _get_walk_for_user(walk_id: str, user: User, db: Session) -> Walk:
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if user.role not in {"admin", "super_admin"} and walk.tutor_id != user.id and walk.walker_id != user.id and walk.assigned_walker_id != user.id:
        raise HTTPException(status_code=403, detail="Sem permissao")
    return walk


def _refresh_reliability_events(walks: list[Walk], db: Session) -> None:
    created = False
    for walk in walks:
        created = bool(detect_reliability_events(walk, db)) or created
    if created:
        db.commit()

@router.get("", response_model=list[WalkResponse])
def list_walks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    query = db.query(Walk)
    if user.role == "walker":
        query = query.filter((Walk.walker_id == user.id) | (Walk.walker_id.is_(None)))
    elif user.role not in {"admin", "super_admin"}:
        query = query.filter(Walk.tutor_id == user.id)
    walks = query.order_by(Walk.created_at.desc()).all()
    _refresh_reliability_events(walks, db)
    return [serialize_operational_walk(walk, db, user=user) for walk in walks]

@router.post("", response_model=WalkResponse)
def create_walk(payload: WalkCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = payload.model_dump()
    selected_walker_id = data.pop("walker_id", None)
    requested_selection_mode = data.pop("walker_selection_mode", None)
    walker_selection_mode = "only_selected" if requested_selection_mode == "only_selected" else "auto"
    walk = Walk(
        id=str(uuid4()),
        tutor_id=user.id,
        walker_id=selected_walker_id,
        assigned_walker_id=selected_walker_id,
        walker_selection_mode=walker_selection_mode,
        operational_status="pending_walker_confirmation",
        current_attempt=0,
        max_attempts=3,
        **data,
    )
    db.add(walk)
    start_matching(walk, db, actor=user)
    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)

@router.get("/{walk_id}", response_model=WalkResponse)
def get_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    walk = _get_walk_for_user(walk_id, user, db)
    _refresh_reliability_events([walk], db)
    return serialize_operational_walk(walk, db, user=user)

@router.put("/{walk_id}/status", response_model=WalkResponse)
def update_status(walk_id: str, payload: WalkUpdateStatus, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if payload.status in DIRECT_COMPLETION_STATUSES:
        raise HTTPException(status_code=400, detail="Finalização deve ocorrer via revisão operacional.")
    update_operational_status(walk, payload.status, db, actor=user)
    record_late_cancellation_if_applicable(walk, db)
    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)


@router.post("/{walk_id}/review")
def create_walk_review(walk_id: str, payload: WalkReviewCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if walk.tutor_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode avaliar.")
    if walk.operational_status not in REVIEWABLE_COMPLETION_STATUSES:
        raise HTTPException(status_code=409, detail="Avaliação disponível apenas após finalização operacional aprovada.")
    if not walk.walker_id and not walk.assigned_walker_id:
        raise HTTPException(status_code=400, detail="Avaliação exige passeador atribuído ao passeio.")

    completion_review = (
        db.query(WalkCompletionReview)
        .filter(WalkCompletionReview.walk_id == walk.id, WalkCompletionReview.status == "approved")
        .order_by(WalkCompletionReview.reviewed_at.desc())
        .first()
    )
    if not completion_review:
        raise HTTPException(status_code=409, detail="Avaliação exige finalização aprovada pela revisão operacional.")

    existing_review = db.query(WalkReview).filter(WalkReview.walk_id == walk.id).first()
    if existing_review:
        raise HTTPException(status_code=409, detail="Este passeio já possui avaliação registrada.")

    tags = []
    for tag in payload.tags or []:
        normalized = str(tag).strip()
        if normalized and normalized in ALLOWED_WALK_REVIEW_TAGS and normalized not in tags:
            tags.append(normalized)

    review = WalkReview(
        id=str(uuid4()),
        walk_id=walk.id,
        tutor_id=user.id,
        walker_id=walk.walker_id or walk.assigned_walker_id,
        rating=payload.rating,
        comment=(payload.comment or "").strip() or None,
        tags_json=json.dumps(tags),
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    db.refresh(walk)
    return {
        "ok": True,
        "review": _serialize_walk_review(review),
        "walk": serialize_operational_walk(walk, db, user=user),
    }


@router.post("/{walk_id}/tip-checkout")
def create_walk_tip_checkout(walk_id: str, payload: WalkTipCheckoutCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if walk.tutor_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode enviar gorjeta.")
    if walk.operational_status != "ride_completed":
        raise HTTPException(status_code=409, detail="Gorjeta disponível apenas após finalização operacional aprovada.")
    if not _approved_completion_review_exists(walk.id, db):
        raise HTTPException(status_code=409, detail="Gorjeta exige finalização aprovada pela revisão operacional.")

    walker_id = walk.walker_id or walk.assigned_walker_id
    if not walker_id:
        raise HTTPException(status_code=400, detail="Gorjeta exige passeador atribuído ao passeio.")

    recent_cutoff = datetime.utcnow() - timedelta(minutes=15)
    duplicate_paid = (
        db.query(WalkTip)
        .filter(
            WalkTip.walk_id == walk.id,
            WalkTip.tutor_id == user.id,
            WalkTip.walker_id == walker_id,
            WalkTip.amount == float(payload.amount),
            WalkTip.status == "paid",
            WalkTip.paid_at >= recent_cutoff,
        )
        .first()
    )
    if duplicate_paid:
        raise HTTPException(status_code=409, detail="Gorjeta idêntica já confirmada recentemente.")

    tip = WalkTip(
        id=str(uuid4()),
        walk_id=walk.id,
        tutor_id=user.id,
        walker_id=walker_id,
        amount=float(payload.amount),
        status="pending",
        provider=TIP_PROVIDER,
    )
    tip.checkout_url = f"aumigao://tip-checkout/{tip.id}?status=pending"
    db.add(tip)
    db.commit()
    db.refresh(tip)
    return {
        **_serialize_walk_tip(tip),
        "checkout_url": tip.checkout_url,
        "status": tip.status,
        "tip_id": tip.id,
    }


@router.get("/tips/{tip_id}/status")
def get_walk_tip_status(tip_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tip = db.get(WalkTip, tip_id)
    if not tip:
        raise HTTPException(status_code=404, detail="Gorjeta não encontrada.")
    if tip.tutor_id != user.id and tip.walker_id != user.id and user.role not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Sem permissão para consultar esta gorjeta.")
    return {
        **_serialize_walk_tip(tip),
        "tip_id": tip.id,
        "payment_status": tip.status,
    }


@router.post("/{walk_id}/reconfirmation")
def respond_walk_reconfirmation(
    walk_id: str,
    payload: WalkReconfirmationDecision,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if str(walk.tutor_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode reconfirmar.")

    allowed_statuses = {
        "awaiting_tutor_reconfirmation",
        "no_walker_found",
        "matching_failed",
        "auto_rematching",
    }
    previous_status = walk.operational_status
    if previous_status not in allowed_statuses:
        raise HTTPException(status_code=409, detail="Passeio nao aguarda decisao do tutor.")

    action = (payload.action or payload.decision or "").strip()
    if action == "reschedule":
        action = "keep_waiting"

    log_metadata = {"action": action, "previous_status": previous_status}

    if action == "continue_search":
        walk.walker_selection_mode = "auto"
        walk.walker_id = None
        walk.assigned_walker_id = None
        walk.no_walker_reason = None
        walk.matching_finished_at = None
        walk.confirmation_expires_at = None
        log_event(db, walk.id, "tutor_reconfirmation_action", actor_type="tutor", actor_id=user.id, metadata=log_metadata)
        start_matching(walk, db, actor=user)
    elif action in {"keep_waiting", "accept_reschedule"}:
        log_event(db, walk.id, "tutor_reconfirmation_action", actor_type="tutor", actor_id=user.id, metadata=log_metadata)
    elif action == "cancel":
        raise HTTPException(status_code=400, detail="Use o fluxo de cancelamento existente para cancelar este passeio.")
    else:
        raise HTTPException(status_code=400, detail="Acao de reconfirmacao invalida.")

    db.commit()
    db.refresh(walk)
    response = serialize_operational_walk(walk, db, user=user)
    if action in {"keep_waiting", "accept_reschedule"}:
        response["reconfirmation_message"] = "Decisao registrada. Nenhuma remarcacao automatica foi criada neste fluxo."
    return response


@router.post("/{walk_id}/reschedule-selected-walker")
def reschedule_selected_walker_walk(
    walk_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    forbidden = FORBIDDEN_RESCHEDULE_FIELDS.intersection(payload.keys())
    if forbidden:
        raise HTTPException(status_code=400, detail="Esta remarcacao permite alterar apenas data e horario.")

    scheduled_date = str(payload.get("scheduled_date") or "").strip()
    scheduled_at = _parse_scheduled_at(scheduled_date)
    if scheduled_at <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="Escolha um horario futuro para remarcar.")

    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if str(walk.tutor_id) != str(user.id):
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode remarcar.")
    if walk.operational_status != "awaiting_tutor_reconfirmation":
        raise HTTPException(status_code=409, detail="Passeio nao aguarda remarcacao operacional.")
    if (walk.walker_selection_mode or "auto") != "only_selected":
        raise HTTPException(status_code=409, detail="Remarcacao restrita disponivel apenas para passeador escolhido.")

    selected_walker_id = walk.assigned_walker_id or walk.walker_id
    if not selected_walker_id:
        raise HTTPException(status_code=400, detail="Passeio sem passeador escolhido para remarcacao.")

    pending_attempt = (
        db.query(WalkMatchingAttempt)
        .filter(WalkMatchingAttempt.walk_id == walk.id, WalkMatchingAttempt.status == "pending")
        .first()
    )
    if pending_attempt:
        return serialize_operational_walk(walk, db, user=user)

    previous_scheduled_date = walk.scheduled_date
    walk.scheduled_date = scheduled_date

    if hasattr(walk, "walk_date"):
        walk.walk_date = payload.get("walk_date")

    if hasattr(walk, "walk_time"):
        walk.walk_time = payload.get("walk_time")

    walk.walker_id = selected_walker_id
    walk.assigned_walker_id = selected_walker_id
    walk.walker_selection_mode = "only_selected"
    walk.operational_status = "pending_walker_confirmation"
    walk.status = "Agendado"
    walk.no_walker_reason = None
    walk.matching_finished_at = None
    walk.confirmation_expires_at = None

    log_event(
        db,
        walk.id,
        "selected_walker_reschedule_requested",
        actor_type="tutor",
        actor_id=user.id,
        metadata={
            "previous_scheduled_date": previous_scheduled_date,
            "scheduled_date": scheduled_date,
            "walk_date": payload.get("walk_date"),
            "walk_time": payload.get("walk_time"),
            "walker_id": selected_walker_id,
        },
    )

    start_matching(walk, db, actor=user)

    db.commit()
    db.refresh(walk)

    return serialize_operational_walk(walk, db, user=user)


@router.post("/{walk_id}/tip")
def create_walk_tip(walk_id: str, payload: WalkTipCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return create_walk_tip_checkout(
        walk_id,
        WalkTipCheckoutCreate(amount=payload.amount),
        user,
        db,
    )

@router.delete("/{walk_id}")
def delete_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    db.delete(walk)
    db.commit()
    return {"ok": True}


@router.post("/{walk_id}/complaint")
def create_walk_complaint(walk_id: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    complaint_payload = ComplaintCreate(
        source="tutor",
        target_type=payload.get("target_type") or "walker",
        target_user_id=payload.get("target_user_id") or walk.walker_id,
        target_pet_id=payload.get("target_pet_id") or walk.pet_id,
        walk_id=walk.id,
        category=payload.get("category") or "servico",
        title=payload.get("title") or "Reclamacao sobre passeio",
        description=payload.get("description") or payload.get("notes") or "Tutor registrou uma ocorrencia sobre o passeio.",
        evidences=[ComplaintEvidenceCreate(**item) for item in payload.get("evidences", [])],
        metadata={"origin": "walk_detail", **(payload.get("metadata") or {})},
    )
    return create_complaint(complaint_payload, user, db)


@router.post("/{walk_id}/kit-issue-report")
def create_walk_kit_issue_report(walk_id: str, payload: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if not payload.get("confirm_report"):
        raise HTTPException(status_code=400, detail="Confirme a ocorrencia antes de enviar.")
    missing = ", ".join([key for key, value in (payload.get("missing_items") or {}).items() if not value]) or "Itens essenciais do kit"
    complaint_payload = ComplaintCreate(
        source="tutor",
        target_type="walker",
        target_user_id=walk.walker_id,
        target_pet_id=walk.pet_id,
        walk_id=walk.id,
        category="falta_cuidado",
        title="Ocorrencia de kit do passeador",
        description=payload.get("notes") or f"Tutor informou problema com kit: {missing}.",
        evidences=[],
        metadata={"origin": "kit_issue_report", "missing_items": payload.get("missing_items") or {}},
    )
    return create_complaint(complaint_payload, user, db)
