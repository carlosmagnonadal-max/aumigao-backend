import logging
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.models.walker_weekly_mission import WalkerWeeklyMission
from app.services.reputation_service import COMPLETED_STATUSES

_logger = logging.getLogger("aumigao.weekly_missions")

MISSION_TEMPLATES = [
    {
        "mission_type": "completed_walks",
        "title": "Conclua 10 passeios",
        "description": "Complete 10 passeios nesta semana para fortalecer sua evolucao.",
        "metric_key": "completed_walks_week",
        "target_value": 10.0,
    },
    {
        "mission_type": "rating",
        "title": "Mantenha uma boa avaliacao",
        "description": "Finalize a semana com avaliacao media acima de 4,7.",
        "metric_key": "average_rating_week",
        "target_value": 4.7,
    },
    {
        "mission_type": "active_days",
        "title": "Fique disponivel na semana",
        "description": "Fique disponivel em pelo menos 4 dias desta semana.",
        "metric_key": "active_days_week",
        "target_value": 4.0,
    },
    {
        "mission_type": "cancellations",
        "title": "Evite cancelamentos",
        "description": "Cuide da sua agenda e evite cancelar passeios aceitos.",
        "metric_key": "cancellations_week",
        "target_value": 0.0,
    },
    {
        "mission_type": "response_time",
        "title": "Responda com agilidade",
        "description": "Responda rapidamente as novas solicitacoes recebidas.",
        "metric_key": "fast_response_rate_week",
        "target_value": 80.0,
    },
]


def get_current_week_range(reference: datetime | None = None) -> tuple[datetime, datetime]:
    now = reference or datetime.utcnow()
    week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
    week_end = week_start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return week_start, week_end


def parse_walk_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def ensure_approved_walker(user: User, db: Session) -> WalkerProfile:
    if user.role not in {"walker", "passeador"}:
        raise HTTPException(status_code=403, detail="Apenas passeadores podem acessar missoes semanais.")
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == user.id).first()
    if not profile or profile.status not in {"approved", "active"}:
        raise HTTPException(status_code=403, detail="Missoes semanais ficam disponiveis para passeadores aprovados.")
    return profile


def get_walks_in_week(walker_id: str, week_start: datetime, week_end: datetime, db: Session) -> list[Walk]:
    walks = db.query(Walk).filter(Walk.walker_id == walker_id).all()
    result = []
    for walk in walks:
        started_at = parse_walk_datetime(walk.scheduled_date) or walk.created_at
        if started_at and week_start <= started_at <= week_end:
            result.append(walk)
    return result


def weekly_reviews(walker_id: str, week_start: datetime, week_end: datetime, db: Session) -> list[WalkerReview]:
    return (
        db.query(WalkerReview)
        .filter(
            WalkerReview.walker_id == walker_id,
            WalkerReview.created_at >= week_start,
            WalkerReview.created_at <= week_end,
        )
        .all()
    )


def mission_current_value(walker_id: str, mission: WalkerWeeklyMission, db: Session) -> float:
    walks = get_walks_in_week(walker_id, mission.week_start, mission.week_end, db)

    if mission.metric_key == "completed_walks_week":
        return float(len([walk for walk in walks if (walk.status or "").strip() in COMPLETED_STATUSES]))

    if mission.metric_key == "average_rating_week":
        reviews = weekly_reviews(walker_id, mission.week_start, mission.week_end, db)
        if not reviews:
            return 0.0
        return round(sum(review.rating for review in reviews) / len(reviews), 2)

    if mission.metric_key == "active_days_week":
        active_days = {
            (parse_walk_datetime(walk.scheduled_date) or walk.created_at).date().isoformat()
            for walk in walks
            if (parse_walk_datetime(walk.scheduled_date) or walk.created_at)
        }
        return float(len(active_days))

    if mission.metric_key == "cancellations_week":
        return float(len([walk for walk in walks if (walk.status or "").strip().lower() == "cancelado"]))

    if mission.metric_key == "fast_response_rate_week":
        completed = len([walk for walk in walks if (walk.status or "").strip() in COMPLETED_STATUSES])
        # MVP signal until response telemetry exists.
        return float(min(92, 72 + completed * 2))

    return 0.0


def calculate_progress(mission: WalkerWeeklyMission) -> float:
    if mission.metric_key == "cancellations_week":
        if mission.current_value <= mission.target_value:
            return 100.0
        return max(0.0, 100.0 - mission.current_value * 25.0)
    if mission.metric_key == "average_rating_week":
        if mission.current_value <= 0:
            return 0.0
        return min(100.0, round((mission.current_value / mission.target_value) * 100, 2))
    if mission.target_value <= 0:
        return 0.0
    return min(100.0, round((mission.current_value / mission.target_value) * 100, 2))


def update_mission_status(mission: WalkerWeeklyMission, now: datetime | None = None) -> WalkerWeeklyMission:
    current_time = now or datetime.utcnow()
    mission.progress_percentage = calculate_progress(mission)

    if current_time > mission.week_end and mission.progress_percentage < 100:
        mission.status = "expired"
        mission.expired_at = mission.expired_at or current_time
        mission.reward_status = "none"
        return mission

    if mission.progress_percentage >= 100:
        mission.status = "completed"
        mission.completed_at = mission.completed_at or current_time
        mission.expired_at = None
        mission.reward_status = "future_benefit"
        mission.reward_description = "Beneficio futuro preparado para campanhas, selos ou prioridade. Sem pagamento automatico no MVP."
        return mission

    if mission.current_value > 0:
        mission.status = "in_progress"
    else:
        mission.status = "not_started"
    mission.completed_at = None
    mission.expired_at = None
    mission.reward_status = "none"
    mission.reward_description = None
    return mission


def expire_old_missions(walker_id: str, db: Session) -> None:
    now = datetime.utcnow()
    missions = (
        db.query(WalkerWeeklyMission)
        .filter(
            WalkerWeeklyMission.walker_id == walker_id,
            WalkerWeeklyMission.week_end < now,
            WalkerWeeklyMission.status.notin_(["completed", "expired"]),
        )
        .all()
    )
    for mission in missions:
        mission.status = "expired"
        mission.expired_at = now
        mission.reward_status = "none"
    if missions:
        db.commit()


def get_or_create_weekly_missions(walker_id: str, db: Session) -> list[WalkerWeeklyMission]:
    week_start, week_end = get_current_week_range()
    expire_old_missions(walker_id, db)
    missions = (
        db.query(WalkerWeeklyMission)
        .filter(WalkerWeeklyMission.walker_id == walker_id, WalkerWeeklyMission.week_start == week_start)
        .order_by(WalkerWeeklyMission.created_at.asc())
        .all()
    )
    existing_types = {mission.mission_type for mission in missions}
    for template in MISSION_TEMPLATES:
        if template["mission_type"] in existing_types:
            continue
        mission = WalkerWeeklyMission(
            id=str(uuid4()),
            walker_id=walker_id,
            week_start=week_start,
            week_end=week_end,
            current_value=0,
            progress_percentage=0,
            status="not_started",
            reward_status="none",
            **template,
        )
        db.add(mission)
        missions.append(mission)
    db.commit()
    return refresh_weekly_missions(walker_id, db)


def refresh_weekly_missions(walker_id: str, db: Session) -> list[WalkerWeeklyMission]:
    week_start, _ = get_current_week_range()
    missions = (
        db.query(WalkerWeeklyMission)
        .filter(WalkerWeeklyMission.walker_id == walker_id, WalkerWeeklyMission.week_start == week_start)
        .order_by(WalkerWeeklyMission.created_at.asc())
        .all()
    )
    for mission in missions:
        prev_status = mission.status
        mission.current_value = mission_current_value(walker_id, mission, db)
        update_mission_status(mission)

        # ── Gancho C: CR por missão semanal concluída ────────────────────────
        # Dispara apenas na TRANSIÇÃO para "completed" (prev != completed).
        # Idempotente: already_awarded verifica se a missão já ganhou CR.
        if mission.status == "completed" and prev_status != "completed":
            try:
                # Import tardio para evitar ciclo de importação.
                import app.services.walker_cr_service as _cr_svc
                from app.services.walker_cr_rules import CR_EARN
                from app.services.walker_gamification_service import log_event as _gami_log_event

                if not _cr_svc.already_awarded(db, walker_id, "weekly_mission", mission.id):
                    _cr_svc.earn_cr(
                        db,
                        walker_id,
                        CR_EARN["weekly_mission"],
                        "weekly_mission",
                        description=f"Missão semanal '{mission.title}' concluída.",
                        related_entity_type="walker_weekly_mission",
                        related_entity_id=mission.id,
                    )
                    _gami_log_event(
                        db,
                        walker_id,
                        event_type="mission_completed",
                        title=f"Missão concluída: {mission.title}",
                        description=f"Missão semanal concluída com {mission.current_value}/{mission.target_value}.",
                        related_entity_type="walker_weekly_mission",
                        related_entity_id=mission.id,
                    )
            except Exception as _cr_exc:
                _logger.warning(
                    "Gancho CR weekly_mission falhou (mission=%s): %s",
                    mission.id,
                    _cr_exc,
                )

    db.commit()
    return missions


def mission_message(mission: WalkerWeeklyMission) -> str:
    if mission.status == "completed":
        return "Missao concluida. Excelente evolucao nesta semana."
    if mission.status == "expired":
        return "Esta missao ficou no historico. Uma nova semana traz novas oportunidades."
    if mission.metric_key == "cancellations_week" and mission.current_value <= mission.target_value:
        return "Voce esta cuidando bem da sua agenda."
    return "Continue assim para desbloquear beneficios futuros."


def mission_payload(mission: WalkerWeeklyMission) -> dict:
    return {
        "id": mission.id,
        "walker_id": mission.walker_id,
        "mission_type": mission.mission_type,
        "title": mission.title,
        "description": mission.description,
        "metric_key": mission.metric_key,
        "target_value": mission.target_value,
        "current_value": mission.current_value,
        "progress_percentage": mission.progress_percentage,
        "status": mission.status,
        "week_start": mission.week_start,
        "week_end": mission.week_end,
        "reward_status": mission.reward_status,
        "reward_description": mission.reward_description,
        "created_at": mission.created_at,
        "updated_at": mission.updated_at,
        "completed_at": mission.completed_at,
        "expired_at": mission.expired_at,
        "motivational_message": mission_message(mission),
    }


def get_walker_weekly_missions(walker_id: str, db: Session) -> dict:
    missions = get_or_create_weekly_missions(walker_id, db)
    week_start, week_end = get_current_week_range()
    return {"week_start": week_start, "week_end": week_end, "missions": [mission_payload(mission) for mission in missions]}


def get_walker_mission_summary(walker_id: str, db: Session) -> dict:
    payload = get_walker_weekly_missions(walker_id, db)
    missions = payload["missions"]
    completed = len([mission for mission in missions if mission["status"] == "completed"])
    in_progress = len([mission for mission in missions if mission["status"] == "in_progress"])
    expired = len([mission for mission in missions if mission["status"] == "expired"])
    progress = round(sum(float(mission["progress_percentage"] or 0) for mission in missions) / len(missions), 2) if missions else 0.0

    if missions and completed == len(missions):
        message = "Voce concluiu todas as missoes desta semana. Excelente evolucao!"
    elif completed:
        message = "Cada bom passeio fortalece sua jornada no Aumigao."
    else:
        message = "Acompanhe desafios leves para fortalecer sua reputacao no Aumigao."

    return {
        "total_missions": len(missions),
        "completed_missions": completed,
        "in_progress_missions": in_progress,
        "expired_missions": expired,
        "week_start": payload["week_start"],
        "week_end": payload["week_end"],
        "motivational_message": message,
        "progress_percentage": progress,
    }


def get_admin_walker_weekly_missions(walker_id: str, db: Session) -> dict:
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == walker_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Passeador nao encontrado.")
    payload = get_walker_weekly_missions(walker_id, db)
    history_rows = (
        db.query(WalkerWeeklyMission)
        .filter(WalkerWeeklyMission.walker_id == walker_id, WalkerWeeklyMission.week_start < payload["week_start"])
        .order_by(WalkerWeeklyMission.week_start.desc(), WalkerWeeklyMission.created_at.asc())
        .limit(20)
        .all()
    )
    return {
        **payload,
        "walker_id": walker_id,
        "walker_name": profile.full_name or "Passeador Aumigao",
        "status": profile.status,
        "history": [mission_payload(mission) for mission in history_rows],
    }
