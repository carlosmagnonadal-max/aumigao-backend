from datetime import datetime
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.services.reputation_service import calculate_basic_behavior_score, reputation_summary, walker_reviews_query


def alert_payload(alert: WalkerMonitoringAlert) -> dict:
    return {
        "id": alert.id,
        "walker_id": alert.walker_id,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "title": alert.title,
        "description": alert.description,
        "status": alert.status,
        "source": alert.source,
        "created_at": alert.created_at,
        "resolved_at": alert.resolved_at,
        "reviewed_by_admin_id": alert.reviewed_by_admin_id,
        "admin_notes": alert.admin_notes,
    }


def create_alert(walker_id: str, alert_type: str, severity: str, title: str, description: str, source: str, db: Session) -> WalkerMonitoringAlert:
    existing = (
        db.query(WalkerMonitoringAlert)
        .filter(WalkerMonitoringAlert.walker_id == walker_id, WalkerMonitoringAlert.alert_type == alert_type, WalkerMonitoringAlert.status.in_(["open", "in_review"]))
        .first()
    )
    if existing:
        return existing

    alert = WalkerMonitoringAlert(
        id=str(uuid4()),
        walker_id=walker_id,
        alert_type=alert_type,
        severity=severity,
        title=title,
        description=description,
        source=source,
        status="open",
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return alert


def evaluate_monitoring_alerts(walker_id: str, db: Session) -> list[WalkerMonitoringAlert]:
    summary = reputation_summary(walker_id, db)
    behavior = calculate_basic_behavior_score(walker_id, db)
    reviews = walker_reviews_query(walker_id, db).limit(6).all()
    negative_reviews = len([review for review in reviews if review.rating <= 3])

    if summary["reviews_count"] >= 3 and summary["rating_average"] < 4.3:
        create_alert(
            walker_id,
            "low_rating",
            "high" if summary["rating_average"] < 4.0 else "medium",
            "Avaliacao abaixo da faixa saudavel",
            "Acompanhar comentarios recentes e orientar melhoria antes de qualquer acao mais forte.",
            "reputation",
            db,
        )

    if behavior["cancellation_rate"] >= 12:
        create_alert(
            walker_id,
            "high_cancellation",
            "high" if behavior["cancellation_rate"] >= 25 else "medium",
            "Cancelamentos acima do ideal",
            "Revisar agenda e orientar aceite apenas quando houver seguranca de disponibilidade.",
            "system",
            db,
        )

    if negative_reviews >= 2:
        create_alert(
            walker_id,
            "negative_reviews",
            "medium",
            "Comentarios recentes pedem acompanhamento",
            "Avaliacoes recentes indicam pontos para melhoria da experiencia do tutor.",
            "review",
            db,
        )

    return (
        db.query(WalkerMonitoringAlert)
        .filter(WalkerMonitoringAlert.walker_id == walker_id)
        .order_by(WalkerMonitoringAlert.created_at.desc())
        .all()
    )


def open_alerts(walker_id: str, db: Session) -> list[WalkerMonitoringAlert]:
    return (
        db.query(WalkerMonitoringAlert)
        .filter(WalkerMonitoringAlert.walker_id == walker_id, WalkerMonitoringAlert.status.in_(["open", "in_review"]))
        .order_by(WalkerMonitoringAlert.created_at.desc())
        .all()
    )


def update_alert(alert_id: str, status: str, admin_notes: str | None, admin_id: str | None, db: Session) -> WalkerMonitoringAlert:
    alert = db.get(WalkerMonitoringAlert, alert_id)
    if not alert:
        raise HTTPException(status_code=404, detail="Alerta nao encontrado")
    alert.status = status
    alert.admin_notes = admin_notes or alert.admin_notes
    alert.reviewed_by_admin_id = admin_id
    if status in {"resolved", "dismissed"}:
        alert.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(alert)
    return alert
