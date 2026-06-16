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
            "score_policy": "Indicador informativo de desempenho. Não gera bloqueios automáticos.",
        }

    rating_avg, rating_count = _rating_summary(walker_id, db)
    return _score_from_inputs(
        completed_count=len(_completed_walks(walker_id, db)),
        rating_avg=rating_avg,
        rating_count=rating_count,
        events=_recent_events(walker_id, db),
        rejected_count=_rejected_completion_count(walker_id, db),
    )


def _score_from_inputs(
    completed_count: int,
    rating_avg: float,
    rating_count: int,
    events: list,
    rejected_count: int,
) -> dict:
    """Lógica PURA de score (sem I/O) — compartilhada pelo cálculo single e o batch.

    Mantém o cálculo idêntico ao anterior; só foi extraída para permitir alimentar com
    dados pré-carregados em lote (calculate_walker_operational_scores) e eliminar o N+1.
    """
    attention_events = [event for event in events if event.event_type in ATTENTION_EVENTS]
    high_attention_events = [event for event in events if event.event_type in HIGH_ATTENTION_EVENTS or event.severity == "high"]

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
        "score_policy": "Indicador informativo de desempenho. Não gera bloqueios automáticos.",
    }


def calculate_walker_operational_scores(walker_ids, db: Session) -> dict[str, dict]:
    """B-ALT-006 follow-up: score operacional de VÁRIOS passeadores SEM N+1.

    Faz 4 queries agrupadas (passeios concluídos, avaliações, eventos recentes,
    finalizações rejeitadas) em vez de 4 por passeador, e reaproveita _score_from_inputs
    para um resultado IDÊNTICO ao calculate_walker_operational_score por passeador.
    Retorna {walker_id: payload}. IDs None/duplicados são ignorados.
    """
    ids = [wid for wid in dict.fromkeys(walker_ids) if wid]
    if not ids:
        return {}
    idset = set(ids)

    # 1) passeios concluídos — atribui cada passeio aos walkers (walker_id/assigned) no conjunto.
    completed_ids: dict[str, set] = {wid: set() for wid in ids}
    walks = (
        db.query(Walk)
        .filter(
            ((Walk.walker_id.in_(idset)) | (Walk.assigned_walker_id.in_(idset))),
            ((Walk.operational_status == "ride_completed") | (Walk.status == "Finalizado")),
        )
        .all()
    )
    for walk in walks:
        for wid in {walk.walker_id, walk.assigned_walker_id} & idset:
            completed_ids[wid].add(walk.id)

    # 2) avaliações
    ratings: dict[str, list] = {wid: [] for wid in ids}
    for review in db.query(WalkReview).filter(WalkReview.walker_id.in_(idset)).all():
        if review.walker_id in idset:
            ratings[review.walker_id].append(float(review.rating or 0))

    # 3) eventos operacionais recentes (90 dias)
    since = datetime.utcnow() - timedelta(days=90)
    events_by: dict[str, list] = {wid: [] for wid in ids}
    for ev in (
        db.query(WalkOperationalEvent)
        .filter(WalkOperationalEvent.walker_id.in_(idset), WalkOperationalEvent.created_at >= since)
        .all()
    ):
        if ev.walker_id in idset:
            events_by[ev.walker_id].append(ev)

    # 4) finalizações rejeitadas
    rejected_by: dict[str, int] = {wid: 0 for wid in ids}
    for rr in (
        db.query(WalkCompletionReview)
        .filter(
            WalkCompletionReview.walker_user_id.in_(idset),
            WalkCompletionReview.status.in_(["rejected", "completion_rejected"]),
        )
        .all()
    ):
        if rr.walker_user_id in idset:
            rejected_by[rr.walker_user_id] += 1

    result: dict[str, dict] = {}
    for wid in ids:
        rlist = ratings[wid]
        rating_avg = round(sum(rlist) / len(rlist), 2) if rlist else 0
        result[wid] = _score_from_inputs(
            completed_count=len(completed_ids[wid]),
            rating_avg=rating_avg,
            rating_count=len(rlist),
            events=events_by[wid],
            rejected_count=rejected_by[wid],
        )
    return result
