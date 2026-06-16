from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.rbac import require_permission
from app.dependencies.tenant_scope import apply_tenant_filter, get_admin_tenant_scope
from app.models.complaint import Complaint, RiskScore
from app.models.user import User
from app.schemas.complaint import (
    ComplaintAdminUpdate,
    ComplaintCreate,
    ComplaintDecisionReview,
    ComplaintListResponse,
    ComplaintResponse,
    RiskScoreResponse,
)
from app.schemas.metrics import ComplaintMetricsResponse
from app.services.complaint_service import (
    admin_review_decision,
    admin_update_complaint,
    complaint_admin_payload,
    create_complaint,
    get_complaint_or_403,
    list_complaints_for_user,
)
from app.services.admin_operational_event_service import record_admin_operational_event
from app.services.metrics_service import get_complaint_metrics

router = APIRouter(prefix="/complaints", tags=["complaints"])
api_router = APIRouter(prefix="/api/complaints", tags=["complaints"])
admin_router = APIRouter(prefix="/admin/complaints", tags=["admin-complaints"], dependencies=[Depends(require_permission("occurrences.read"))])
api_admin_router = APIRouter(prefix="/api/admin/complaints", tags=["admin-complaints"], dependencies=[Depends(require_permission("occurrences.read"))])
legacy_admin_occurrences_router = APIRouter(prefix="/admin/occurrences", tags=["admin-complaints"], dependencies=[Depends(require_permission("occurrences.read"))])
api_legacy_admin_occurrences_router = APIRouter(prefix="/api/admin/occurrences", tags=["admin-complaints"], dependencies=[Depends(require_permission("occurrences.read"))])


def _list_my(user: User, db: Session):
    items = list_complaints_for_user(user, db)
    return {"items": items, "total": len(items)}


@router.post("", response_model=ComplaintResponse)
@api_router.post("", response_model=ComplaintResponse)
def create_case(payload: ComplaintCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return create_complaint(payload, user, db)


@router.get("", response_model=ComplaintListResponse)
@api_router.get("", response_model=ComplaintListResponse)
def list_my_cases(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _list_my(user, db)


@router.get("/{complaint_id}", response_model=ComplaintResponse)
@api_router.get("/{complaint_id}", response_model=ComplaintResponse)
def get_case(complaint_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return get_complaint_or_403(complaint_id, user, db)


@admin_router.get("/metrics", response_model=ComplaintMetricsResponse)
@api_admin_router.get("/metrics", response_model=ComplaintMetricsResponse)
def admin_complaint_metrics(
    admin: User = Depends(require_permission("occurrences.read")),
    db: Session = Depends(get_db),
):
    """Métricas de ocorrências: contadores, médias, breakdown e série semanal.

    Tenant-scoped (Complaint.tenant_id). avg_resolution_hours é null se não houver
    ocorrências com resolved_at preenchido.
    """
    scope = get_admin_tenant_scope(admin)
    data = get_complaint_metrics(db, scope)
    return ComplaintMetricsResponse(**data)


@admin_router.get("", response_model=ComplaintListResponse)
@api_admin_router.get("", response_model=ComplaintListResponse)
def admin_list_cases(
    status: str | None = Query(None),
    severity: str | None = Query(None),
    category: str | None = Query(None),
    user_id: str | None = Query(None),
    pet_id: str | None = Query(None),
    walker_id: str | None = Query(None),
    walk_id: str | None = Query(None),
    admin: User = Depends(require_permission("occurrences.read")),
    db: Session = Depends(get_db),
):
    query = apply_tenant_filter(db.query(Complaint), Complaint, get_admin_tenant_scope(admin))
    if status and status != "all":
        query = query.filter(Complaint.status == status)
    if severity and severity != "all":
        query = query.filter(Complaint.severity == severity)
    if category:
        query = query.filter(Complaint.category == category)
    if user_id:
        query = query.filter((Complaint.author_id == user_id) | (Complaint.target_user_id == user_id))
    if pet_id:
        query = query.filter(Complaint.target_pet_id == pet_id)
    if walker_id:
        query = query.filter(Complaint.target_user_id == walker_id)
    if walk_id:
        query = query.filter(Complaint.walk_id == walk_id)
    items = query.order_by(Complaint.created_at.desc()).all()
    return {"items": items, "total": len(items)}


@admin_router.get("/{complaint_id}", response_model=ComplaintResponse)
@api_admin_router.get("/{complaint_id}", response_model=ComplaintResponse)
def admin_get_case(complaint_id: str, admin: User = Depends(require_permission("occurrences.read")), db: Session = Depends(get_db)):
    return get_complaint_or_403(complaint_id, admin, db)


@admin_router.patch("/{complaint_id}", response_model=ComplaintResponse)
@api_admin_router.patch("/{complaint_id}", response_model=ComplaintResponse)
def admin_update_case(complaint_id: str, payload: ComplaintAdminUpdate, admin: User = Depends(require_permission("occurrences.manage")), db: Session = Depends(get_db)):
    complaint = get_complaint_or_403(complaint_id, admin, db)
    updated = admin_update_complaint(complaint, payload.status, payload.severity, payload.internal_note, admin, db)
    record_admin_operational_event(
        db,
        event_type="status_changed" if payload.status else "escalated",
        entity_type="complaint",
        entity_id=updated.id,
        severity=updated.severity,
        title="Ocorrencia atualizada",
        description=payload.internal_note or "Ocorrencia atualizada pela operacao administrativa.",
        actor=admin,
        source="admin.complaint.update",
        metadata={"status": updated.status, "severity": updated.severity},
    )
    db.commit()
    return updated


@admin_router.post("/{complaint_id}/decision", response_model=ComplaintResponse)
@api_admin_router.post("/{complaint_id}/decision", response_model=ComplaintResponse)
def admin_decide_case(complaint_id: str, payload: ComplaintDecisionReview, admin: User = Depends(require_permission("occurrences.manage")), db: Session = Depends(get_db)):
    complaint = get_complaint_or_403(complaint_id, admin, db)
    updated = admin_review_decision(complaint, payload.decision_type, payload.decision_status, payload.reason, admin, db)
    record_admin_operational_event(
        db,
        event_type="complaint_decided",
        entity_type="complaint",
        entity_id=updated.id,
        severity=updated.severity,
        title="Decisao de ocorrencia registrada",
        description=payload.reason,
        actor=admin,
        source="admin.complaint.decision",
        metadata={"decision_type": payload.decision_type, "decision_status": payload.decision_status},
    )
    db.commit()
    return updated


@admin_router.get("/risk-scores/list", response_model=list[RiskScoreResponse])
@api_admin_router.get("/risk-scores/list", response_model=list[RiskScoreResponse])
def admin_risk_scores(subject_type: str | None = Query(None), db: Session = Depends(get_db)):
    query = db.query(RiskScore)
    if subject_type:
        query = query.filter(RiskScore.subject_type == subject_type)
    return query.order_by(RiskScore.score.desc()).all()


@legacy_admin_occurrences_router.get("")
@api_legacy_admin_occurrences_router.get("")
def admin_legacy_occurrences(
    status: str | None = Query(None),
    query: str | None = Query(None),
    date: str | None = Query(None),
    region: str | None = Query(None),
    walker_id: str | None = Query(None),
    db: Session = Depends(get_db),
):
    cases = db.query(Complaint)
    if status and status != "all":
        cases = cases.filter(Complaint.status == status)
    if walker_id:
        cases = cases.filter(Complaint.target_user_id == walker_id)
    items = cases.order_by(Complaint.created_at.desc()).all()
    return [complaint_admin_payload(item) for item in items]


# api-T2: schema permissivo da acao administrativa legada sobre ocorrencias. Campos
# opcionais espelhando o (payload or {}).get anterior; Pydantic v2 ignora extras, entao
# nenhum payload existente e rejeitado — so ganhamos validacao de tipo e contrato OpenAPI.
class LegacyOccurrenceActionRequest(BaseModel):
    action: str | None = None
    note: str | None = None


@legacy_admin_occurrences_router.post("/{complaint_id}/action")
@api_legacy_admin_occurrences_router.post("/{complaint_id}/action")
def admin_legacy_occurrence_action(complaint_id: str, payload: LegacyOccurrenceActionRequest, admin: User = Depends(require_permission("occurrences.manage")), db: Session = Depends(get_db)):
    complaint = get_complaint_or_403(complaint_id, admin, db)
    action = payload.action or "add_internal_note"
    note = payload.note or f"Acao administrativa: {action}"
    if action == "mark_resolved":
        return complaint_admin_payload(admin_update_complaint(complaint, "resolvida", None, note, admin, db))
    if action == "mark_unresolved":
        return complaint_admin_payload(admin_update_complaint(complaint, "em_analise", None, note, admin, db))
    updated = admin_review_decision(complaint, action, "approved", note, admin, db)
    return complaint_admin_payload(updated)
