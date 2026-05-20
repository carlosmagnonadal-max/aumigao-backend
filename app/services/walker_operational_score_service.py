from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.walk_review import WalkReview

ATTENTION_EVENTS = {
    "walker_late",
    "walker_no_show",
    "missing_checkin",
    "operational_recovery_triggered",
}
HIGH_ATTENTION_EVENTS = {"walker_no_show", "missing_checkin"}


def _completed_walks(walker_id: str, db: Session) -> list[Walk]:
    return (
        db.query(Walk)
        .filter(
            ((Walk.walker_id == walker_id) | (Walk.assigned_walker_id == walker_id)),
            ((Walk.operational_status == "ride_completed") | (Walk.status == "Finalizado")),
        )
        .all()
    )


def _rating_summary(walker_id: str, db: Session) -> tuple[float, int]:
    reviews = db.query(WalkReview).filter(WalkReview.walker_id == walker_id).all()
    count = len(reviews)
    if not count:
        return 0, 0
    return round(sum(float(review.rating or 0) for review in reviews) / count, 2), count


def _recent_events(walker_id: str, db: Session) -> list[WalkOperationalEvent]:
    since = datetime.utcnow() - timedelta(days=90)
    return (
        db.query(WalkOperationalEvent)
        .filter(
            WalkOperationalEvent.walker_id == walker_id,
            WalkOperationalEvent.created_at >= since,
        )
        .all()
    )


def _rejected_completion_count(walker_id: str, db: Session) -> int:
    return (
        db.query(WalkCompletionReview)
        .filter(
            WalkCompletionReview.walker_user_id == walker_id,
            WalkCompletionReview.status.in_(["rejected", "completion_rejected"]),
        )
        .count()
    )


def _reliability_label(score: int, completed_count: int, attention_count: int) -> str:
    if completed_count < 3:
        return "Em formação"
    if attention_count >= 3 or score < 60:
        return "Atenção operacional"
    if score >= 88:
        return "Muito confiável"
    return "Confiável"


def calculate_walker_operational_score(walker_id: str | None, db: Session) -> dict:
    if not walker_id:
        return {
            "operational_score": 0,
            "reliability_label": "Em formação",
            "score_factors": {
                "positivos": [],
                "pontos_de_atencao": ["Score em formação após os primeiros passeios validados."],
            },
            "score_details": {
                "completed_walks": 0,
                "rating_avg": 0,
                "rating_count": 0,
                "recent_operational_events": 0,
                "completion_rejections": 0,
            },
            "score_policy": "Indicador informativo para acompanhamento do beta. Não gera bloqueios automáticos.",
        }

    completed = _completed_walks(walker_id, db)
    completed_count = len(completed)
    rating_avg, rating_count = _rating_summary(walker_id, db)
    events = _recent_events(walker_id, db)
    attention_events = [event for event in events if event.event_type in ATTENTION_EVENTS]
    high_attention_events = [event for event in events if event.event_type in HIGH_ATTENTION_EVENTS or event.severity == "high"]
    rejected_count = _rejected_completion_count(walker_id, db)

    score = 70
    score += min(12, completed_count * 2)
    if rating_count:
        score += round((rating_avg - 4) * 8)
    if completed_count >= 10:
        score += 5
    score -= len(attention_events) * 5
    score -= len(high_attention_events) * 4
    score -= rejected_count * 6
    score = max(0, min(100, int(round(score))))

    positivos: list[str] = []
    pontos_de_atencao: list[str] = []

    if completed_count:
        positivos.append(f"{completed_count} passeio(s) concluído(s) com validação operacional.")
    if rating_count:
        positivos.append(f"Média de avaliação {rating_avg:.1f} em {rating_count} avaliação(ões).")
    if completed_count >= 10:
        positivos.append("Histórico operacional consistente no beta.")
    if not positivos:
        positivos.append("Score em formação após os primeiros passeios validados.")

    if attention_events:
        pontos_de_atencao.append(f"{len(attention_events)} evento(s) operacional(is) recente(s) em acompanhamento.")
    if rejected_count:
        pontos_de_atencao.append(f"{rejected_count} finalização(ões) rejeitada(s) para ajuste.")
    if not pontos_de_atencao:
        pontos_de_atencao.append("Sem pontos críticos recentes registrados.")

    return {
        "operational_score": score,
        "reliability_label": _reliability_label(score, completed_count, len(attention_events) + rejected_count),
        "score_factors": {
            "positivos": positivos,
            "pontos_de_atencao": pontos_de_atencao,
        },
        "score_details": {
            "completed_walks": completed_count,
            "rating_avg": rating_avg,
            "rating_count": rating_count,
            "recent_operational_events": len(attention_events),
            "high_attention_events": len(high_attention_events),
            "completion_rejections": rejected_count,
        },
        "score_policy": "Indicador informativo para acompanhamento do beta. Não gera bloqueios automáticos.",
    }
