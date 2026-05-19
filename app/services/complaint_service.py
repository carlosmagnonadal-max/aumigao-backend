import json
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.complaint import Complaint, ComplaintDecision, ComplaintEvidence, ComplaintStatusHistory, RiskScore
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.schemas.complaint import ComplaintCreate


HIGH_RISK_CATEGORIES = {
    "fraude",
    "suspeita_fraude",
    "contratacao_por_fora",
    "comunicacao_inadequada",
    "falta_cuidado",
    "passeio_nao_realizado",
    "agressividade_pet",
    "fuga_pet",
    "mal_estar_pet",
    "endereco_inseguro",
    "tutor_ausente",
}

CRITICAL_TERMS = {
    "agressivo",
    "agressividade",
    "mordida",
    "fugiu",
    "fuga",
    "mal-estar",
    "maus tratos",
    "violencia",
    "fraude",
    "fora do app",
    "contratar por fora",
    "sem coleira",
    "inseguro",
    "ameaça",
}

MEDIUM_TERMS = {
    "atraso",
    "foto",
    "rota",
    "status",
    "comunicacao",
    "comunicação",
    "acesso",
    "endereco",
    "endereço",
    "ausente",
    "informacao omitida",
    "informação omitida",
}

LIGHT_AUTO_ACTIONS = {"open_admin_case", "register_risk_history", "generate_recurrence_alert"}
CRITICAL_ACTIONS = {
    "temporarily_suspend_walker",
    "temporarily_suspend_tutor",
    "block_pet_shared_walk",
    "remove_quality_badge",
    "reduce_walker_ranking",
    "review_refund",
}


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_load(value: str, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _can_access_walk(walk: Walk, user: User) -> bool:
    if user.role in {"admin", "super_admin"}:
        return True
    return walk.tutor_id == user.id or walk.walker_id == user.id or walk.assigned_walker_id == user.id


def validate_complaint_access(payload: ComplaintCreate, user: User, db: Session) -> Walk | None:
    if payload.source == "tutor" and user.role not in {"tutor", "cliente", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Apenas tutores podem abrir esta reclamacao.")
    if payload.source == "walker" and user.role not in {"walker", "passeador", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Apenas passeadores podem abrir esta ocorrencia.")

    walk = db.get(Walk, payload.walk_id) if payload.walk_id else None
    if payload.walk_id and not walk:
        raise HTTPException(status_code=404, detail="Passeio relacionado nao encontrado.")
    if walk and not _can_access_walk(walk, user):
        raise HTTPException(status_code=403, detail="Sem permissao para registrar caso neste passeio.")

    if payload.target_pet_id:
        pet = db.get(Pet, payload.target_pet_id)
        if not pet:
            raise HTTPException(status_code=404, detail="Pet relacionado nao encontrado.")
        if user.role not in {"admin", "super_admin"} and payload.source == "tutor" and pet.tutor_id != user.id:
            raise HTTPException(status_code=403, detail="Sem permissao para reclamar sobre este pet.")

    return walk


def classify_complaint(payload: ComplaintCreate, db: Session) -> dict:
    text = _normalize(f"{payload.category} {payload.title} {payload.description}")
    score = 10
    score += 18 if payload.category in HIGH_RISK_CATEGORIES else 0
    score += 34 if any(term in text for term in CRITICAL_TERMS) else 0
    score += 18 if any(term in text for term in MEDIUM_TERMS) else 0
    score += 14 if payload.evidences else 0

    since = datetime.utcnow() - timedelta(days=90)
    recurrence_filters = []
    if payload.target_user_id:
        recurrence_filters.append(Complaint.target_user_id == payload.target_user_id)
    if payload.target_pet_id:
        recurrence_filters.append(Complaint.target_pet_id == payload.target_pet_id)
    if recurrence_filters:
        recurrence_count = db.query(Complaint).filter(or_(*recurrence_filters), Complaint.created_at >= since).count()
    else:
        recurrence_count = 0
    score += min(30, recurrence_count * 10)

    if score >= 75:
        severity = "critica"
    elif score >= 55:
        severity = "alta"
    elif score >= 30:
        severity = "media"
    else:
        severity = "baixa"

    suggestions = ["open_admin_case", "register_risk_history"]
    if recurrence_count >= 2:
        suggestions.append("generate_recurrence_alert")
    if "contratacao_por_fora" in payload.category or "fora do app" in text:
        suggestions.append("register_off_platform_attempt")
    if "cupom" in text or "coupon" in text:
        suggestions.append("flag_coupon_abuse_review")
    if payload.source == "tutor" and severity in {"alta", "critica"}:
        suggestions.extend(["review_refund", "reduce_walker_ranking"])
    if payload.source == "walker" and payload.target_type == "pet" and severity in {"alta", "critica"}:
        suggestions.append("block_pet_shared_walk")
    if payload.source == "walker" and payload.target_type == "tutor" and severity == "critica":
        suggestions.append("temporarily_suspend_tutor")
    if payload.source == "tutor" and payload.target_type == "walker" and severity == "critica":
        suggestions.extend(["temporarily_suspend_walker", "remove_quality_badge"])
    if payload.category in {"falta_cuidado", "atraso", "comunicacao_inadequada"} and severity in {"media", "alta", "critica"}:
        suggestions.append("start_quality_recovery")

    applied = [action for action in suggestions if action in LIGHT_AUTO_ACTIONS]
    return {
        "severity": severity,
        "score": min(100, score),
        "recurrence_count": recurrence_count,
        "suggestions": suggestions,
        "applied": applied,
        "requires_manual_review": bool(set(suggestions) & CRITICAL_ACTIONS) or severity in {"alta", "critica"},
    }


def _history(complaint_id: str, from_status: str, to_status: str, note: str, actor: User | None, db: Session):
    db.add(
        ComplaintStatusHistory(
            id=str(uuid4()),
            complaint_id=complaint_id,
            from_status=from_status,
            to_status=to_status,
            note=note,
            actor_id=actor.id if actor else None,
            actor_role=actor.role if actor else "system",
        )
    )


def _upsert_risk(subject_type: str, subject_id: str | None, severity: str, db: Session):
    if not subject_id:
        return
    risk = db.query(RiskScore).filter(RiskScore.subject_type == subject_type, RiskScore.subject_id == subject_id).first()
    if not risk:
        risk = RiskScore(id=str(uuid4()), subject_type=subject_type, subject_id=subject_id)
        db.add(risk)

    severity_points = {"baixa": 5, "media": 12, "alta": 24, "critica": 40}
    risk.score = min(100, float(risk.score or 0) + severity_points.get(severity, 5))
    risk.complaints_count = int(risk.complaints_count or 0) + 1
    if severity == "critica":
        risk.critical_count = int(risk.critical_count or 0) + 1
    risk.shared_walk_blocked = risk.shared_walk_blocked or (subject_type == "pet" and severity in {"alta", "critica"})
    if risk.score >= 75 or risk.critical_count:
        risk.severity = "critico"
    elif risk.score >= 45:
        risk.severity = "alto"
    elif risk.score >= 20:
        risk.severity = "atencao"
    else:
        risk.severity = "normal"
    risk.updated_at = datetime.utcnow()


def _record_decisions(complaint: Complaint, analysis: dict, db: Session):
    for action in analysis["suggestions"]:
        db.add(
            ComplaintDecision(
                id=str(uuid4()),
                complaint_id=complaint.id,
                decision_type=action,
                decision_status="applied" if action in analysis["applied"] else "suggested",
                severity_snapshot=analysis["severity"],
                reason="Classificacao deterministica inicial do motor de governanca.",
                payload_json=_json_dump({"risk_score": analysis["score"], "recurrence_count": analysis["recurrence_count"]}),
            )
        )


def create_complaint(payload: ComplaintCreate, user: User, db: Session) -> Complaint:
    validate_complaint_access(payload, user, db)
    analysis = classify_complaint(payload, db)
    complaint = Complaint(
        id=str(uuid4()),
        source=payload.source,
        author_id=user.id,
        author_role=user.role,
        target_type=payload.target_type,
        target_user_id=payload.target_user_id,
        target_pet_id=payload.target_pet_id,
        walk_id=payload.walk_id,
        category=payload.category,
        severity=analysis["severity"],
        status="em_analise" if analysis["requires_manual_review"] else "aberta",
        title=payload.title or "Ocorrencia registrada",
        description=payload.description,
        risk_score=analysis["score"],
        requires_manual_review=analysis["requires_manual_review"],
        recurrence_count=analysis["recurrence_count"],
        suggested_actions_json=_json_dump(analysis["suggestions"]),
        applied_actions_json=_json_dump(analysis["applied"]),
        metadata_json=_json_dump(payload.metadata),
    )
    db.add(complaint)
    db.flush()

    for evidence in payload.evidences:
        db.add(
            ComplaintEvidence(
                id=str(uuid4()),
                complaint_id=complaint.id,
                evidence_type=evidence.evidence_type,
                url=evidence.url,
                description=evidence.description,
                created_by_id=user.id,
            )
        )

    _record_decisions(complaint, analysis, db)
    _history(complaint.id, "", complaint.status, "Caso operacional aberto com classificacao inicial.", user, db)

    if payload.target_user_id:
        target_subject = "walker" if payload.target_type == "walker" else "tutor"
        _upsert_risk(target_subject, payload.target_user_id, analysis["severity"], db)
    if payload.target_pet_id:
        _upsert_risk("pet", payload.target_pet_id, analysis["severity"], db)

    db.commit()
    db.refresh(complaint)
    return complaint


def list_complaints_for_user(user: User, db: Session) -> list[Complaint]:
    query = db.query(Complaint)
    if user.role in {"admin", "super_admin"}:
        return query.order_by(Complaint.created_at.desc()).all()
    return (
        query.filter(
            or_(
                Complaint.author_id == user.id,
                Complaint.target_user_id == user.id,
            )
        )
        .order_by(Complaint.created_at.desc())
        .all()
    )


def get_complaint_or_403(complaint_id: str, user: User, db: Session) -> Complaint:
    complaint = db.get(Complaint, complaint_id)
    if not complaint:
        raise HTTPException(status_code=404, detail="Reclamacao/ocorrencia nao encontrada.")
    if user.role in {"admin", "super_admin"} or complaint.author_id == user.id or complaint.target_user_id == user.id:
        return complaint
    raise HTTPException(status_code=403, detail="Sem permissao para acessar este caso.")


def admin_update_complaint(complaint: Complaint, status: str | None, severity: str | None, note: str | None, admin: User, db: Session) -> Complaint:
    previous_status = complaint.status
    if status:
        complaint.status = status
    if severity:
        complaint.severity = severity
    complaint.updated_at = datetime.utcnow()
    if status == "resolvida":
        complaint.resolved_at = datetime.utcnow()
        complaint.resolved_by_admin_id = admin.id
    _history(complaint.id, previous_status, complaint.status, note or "Atualizacao administrativa.", admin, db)
    db.commit()
    db.refresh(complaint)
    return complaint


def admin_review_decision(complaint: Complaint, decision_type: str, decision_status: str, reason: str, admin: User, db: Session) -> Complaint:
    decision = next((item for item in complaint.decisions if item.decision_type == decision_type), None)
    if not decision:
        decision = ComplaintDecision(
            id=str(uuid4()),
            complaint_id=complaint.id,
            decision_type=decision_type,
            severity_snapshot=complaint.severity,
            created_by="admin",
        )
        db.add(decision)
    decision.decision_status = decision_status
    decision.reason = reason
    decision.reviewed_by_admin_id = admin.id
    decision.reviewed_at = datetime.utcnow()
    _history(complaint.id, complaint.status, complaint.status, f"Decisao {decision_type}: {decision_status}. {reason}", admin, db)
    db.commit()
    db.refresh(complaint)
    return complaint


def complaint_admin_payload(complaint: Complaint) -> dict:
    suggestions = _json_load(complaint.suggested_actions_json, [])
    return {
        "id": complaint.id,
        "occurrence_status": complaint.status,
        "summary": complaint.description,
        "client_name": complaint.author_role if complaint.source == "tutor" else complaint.target_user_id or "-",
        "walker_name": complaint.target_user_id if complaint.target_type == "walker" else complaint.author_id,
        "pet_name": complaint.target_pet_id or "-",
        "walk_date": "-",
        "walk_time": "-",
        "region": "",
        "scheduled_start_at": "",
        "walker_check_in_at": "",
        "client_confirmed_at": "",
        "tolerance_expires_at": "",
        "charged_amount": 0,
        "walker_payout_amount": 0,
        "platform_retained_amount": 0,
        "client_refund_amount": 0,
        "tip_amount": 0,
        "financial_status": "sem_disputa",
        "suspected_disintermediation": "register_off_platform_attempt" in suggestions,
        "severity": complaint.severity,
        "category": complaint.category,
        "risk_score": complaint.risk_score,
        "requires_manual_review": complaint.requires_manual_review,
        "evidence_count": len(complaint.evidences or []),
        "decision_count": len(complaint.decisions or []),
        "logs": [
            {"id": log.id, "timestamp": log.created_at.isoformat(), "action": f"{log.to_status}: {log.note}"}
            for log in complaint.history
        ],
    }
