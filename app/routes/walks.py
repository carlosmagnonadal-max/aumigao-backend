from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.walk import Walk
from app.models.user import User
from app.models.pet import Pet
from app.schemas.walk import WalkCreate, WalkResponse, WalkUpdateStatus
from app.schemas.complaint import ComplaintCreate, ComplaintEvidenceCreate
from app.services.complaint_service import create_complaint
from app.services.operational_matching_service import (
    LEGACY_STATUS_TO_OPERATIONAL,
    RIDE_SCHEDULED,
    process_expired_attempts,
    serialize_operational_walk,
    start_matching,
    update_operational_status,
)

router = APIRouter(prefix="/walks", tags=["walks"])

COMPLETED_WALK_STATUSES = {"Finalizado", "Concluido", "Concluído", "finalizado", "completed", "finished"}


class WalkTipCreate(BaseModel):
    amount: float = Field(gt=0, le=500)
    note: str | None = None


def _split_scheduled_date(value: str) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    date_part, _, time_part = value.partition("T")
    return date_part or None, time_part[:5] or None


def _serialize_walk(walk: Walk, db: Session) -> dict:
    return serialize_operational_walk(walk, db, include_private=True)


def _get_walk_for_user(walk_id: str, user: User, db: Session) -> Walk:
    walk = db.get(Walk, walk_id)
    if not walk:
        raise HTTPException(status_code=404, detail="Passeio nao encontrado")
    if user.role not in {"admin", "super_admin"} and walk.tutor_id != user.id and walk.walker_id != user.id and walk.assigned_walker_id != user.id:
        raise HTTPException(status_code=403, detail="Sem permissao")
    return walk

@router.get("", response_model=list[WalkResponse])
def list_walks(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    query = db.query(Walk)
    if user.role == "walker":
        query = query.filter((Walk.walker_id == user.id) | (Walk.walker_id.is_(None)))
    elif user.role not in {"admin", "super_admin"}:
        query = query.filter(Walk.tutor_id == user.id)
    return [serialize_operational_walk(walk, db, user=user) for walk in query.order_by(Walk.created_at.desc()).all()]

@router.post("", response_model=WalkResponse)
def create_walk(payload: WalkCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    data = payload.model_dump()
    selected_walker_id = data.pop("walker_id", None)
    walk = Walk(
        id=str(uuid4()),
        tutor_id=user.id,
        walker_id=selected_walker_id,
        assigned_walker_id=selected_walker_id,
        operational_status=RIDE_SCHEDULED,
        current_attempt=0,
        max_attempts=3,
        **data,
    )
    db.add(walk)
    if selected_walker_id:
        start_matching(walk, db, actor=user)
    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)

@router.get("/{walk_id}", response_model=WalkResponse)
def get_walk(walk_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    process_expired_attempts(db)
    walk = _get_walk_for_user(walk_id, user, db)
    return serialize_operational_walk(walk, db, user=user)

@router.put("/{walk_id}/status", response_model=WalkResponse)
def update_status(walk_id: str, payload: WalkUpdateStatus, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    update_operational_status(walk, payload.status, db, actor=user)
    db.commit()
    db.refresh(walk)
    return serialize_operational_walk(walk, db, user=user)


@router.post("/{walk_id}/tip")
def create_walk_tip(walk_id: str, payload: WalkTipCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    walk = _get_walk_for_user(walk_id, user, db)
    if walk.tutor_id != user.id:
        raise HTTPException(status_code=403, detail="Apenas o tutor dono do passeio pode registrar gorjeta.")

    status = (walk.status or "").strip()
    if status not in COMPLETED_WALK_STATUSES:
        raise HTTPException(status_code=400, detail="Gorjeta disponivel apenas para passeio finalizado.")

    walker_id = walk.walker_id or walk.assigned_walker_id
    if not walker_id:
        raise HTTPException(status_code=400, detail="Gorjeta exige passeador atribuido ao passeio.")

    payment = Payment(
        id=str(uuid4()),
        tutor_id=user.id,
        walk_id=walk.id,
        amount=float(payload.amount),
        status="tip_registered",
        provider="internal_tip",
    )
    db.add(payment)
    db.commit()
    db.refresh(payment)
    return {
        "id": payment.id,
        "walk_id": walk.id,
        "tutor_id": user.id,
        "walker_id": walker_id,
        "amount": payment.amount,
        "status": payment.status,
        "provider": payment.provider,
        "provider_payment_id": payment.provider_payment_id,
        "created_at": payment.created_at,
        "note": (payload.note or "").strip() or None,
        "requires_payment_capture": False,
        "message": "Gorjeta registrada para conciliacao futura. Nenhuma cobranca real foi feita.",
    }

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
