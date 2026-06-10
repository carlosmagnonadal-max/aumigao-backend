"""Gamificacao do TUTOR computada a partir dos passeios (sem tabela nova).

Espelha EXATAMENTE a logica do front, que hoje calcula isso no cliente:
- frontend/src/lib/walkSelectors.ts (buildTutorGamificationFromWalks, statuses,
  calculateCareStreakDays, selecao de concluidos/agendados)
- frontend/utils/gamification.ts (niveis do tutor, getGamificationLevel,
  buildTutorBadges, buildTutorGamification)

Regras (do front, nao inventadas aqui):
- niveis Tutor iniciante/cuidadoso/dedicado/referencia/premium nos cortes
  0/100/250/500/900 XP
- XP = passeios_concluidos * 45 + agendados * 10
- care_streak = dias consecutivos (a partir de hoje/ontem) com passeio concluido
- badges first_care / dedicated_tutor / care_week / complete_family / premium_tutor
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.walk import Walk
from app.schemas.tutor_gamification import (
    GamificationEvent,
    TutorBadge,
    TutorGamification,
)

ONE_DAY_SECONDS = 24 * 60 * 60

# Status normalizados (lower/trim) — espelha walkSelectors.ts.
FINISHED_STATUSES = {
    "finalizado",
    "concluido",
    "concluído",
    "concluãdo",
    "ride_completed",
    "completed",
}
CANCELLED_STATUSES = {
    "cancelado",
    "cancelada",
    "canceled",
    "cancelled",
    "ride_cancelled",
}
ACTIVE_STATUSES = {
    "passeando agora",
    "ride_in_progress",
}
PICKUP_STATUSES = {
    "indo buscar o pet",
    "walker_arriving",
    "walker_heading_to_pickup",
}
PENDING_ACCEPTANCE_STATUSES = {
    "pending_walker_confirmation",
    "pending_walker_acceptance",
    "walker_confirmation_pending",
}
RECOVERY_STATUSES = {
    "awaiting_tutor_reconfirmation",
    "no_walker_found",
    "walker_declined",
}
INVALID_UPCOMING_STATUSES = FINISHED_STATUSES | CANCELLED_STATUSES | {
    "no_walker_found",
    "walker_declined",
}

# Niveis do tutor — cortes identicos a frontend/utils/gamification.ts (tutorLevels).
TUTOR_LEVELS = [
    {"level": 1, "title": "Tutor iniciante", "minXp": 0, "nextLevelXp": 100},
    {"level": 2, "title": "Tutor cuidadoso", "minXp": 100, "nextLevelXp": 250},
    {"level": 3, "title": "Tutor dedicado", "minXp": 250, "nextLevelXp": 500},
    {"level": 4, "title": "Tutor referencia", "minXp": 500, "nextLevelXp": 900},
    {"level": 5, "title": "Tutor premium", "minXp": 900, "nextLevelXp": None},
]


def _normalize_status(value: str | None) -> str:
    return str(value or "").strip().lower()


def _walk_status(walk: Walk) -> str:
    """Espelha `operational_status || status` do front."""
    return _normalize_status(walk.operational_status or walk.status)


def _legacy_status(walk: Walk) -> str:
    return _normalize_status(walk.status)


def is_cancelled_walk(walk: Walk) -> bool:
    return _walk_status(walk) in CANCELLED_STATUSES or _legacy_status(walk) in CANCELLED_STATUSES


def is_completed_walk(walk: Walk) -> bool:
    return _walk_status(walk) in FINISHED_STATUSES or _legacy_status(walk) in FINISHED_STATUSES


def is_active_walk(walk: Walk) -> bool:
    status = _walk_status(walk)
    legacy = _legacy_status(walk)
    return (
        status in ACTIVE_STATUSES
        or legacy in ACTIVE_STATUSES
        or status in PICKUP_STATUSES
        or legacy in PICKUP_STATUSES
    )


def _parse_walk_datetime(walk: Walk) -> datetime | None:
    """Espelha parseWalkDateTime: usa scheduled_date (ISO). None se invalido."""
    scheduled = str(walk.scheduled_date or "").strip()
    if not scheduled:
        return None
    try:
        return datetime.fromisoformat(scheduled)
    except ValueError:
        return None


def _to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _start_of_local_day_ts(value: datetime) -> float:
    return datetime(value.year, value.month, value.day).timestamp()


def calculate_care_streak_days(completed_walks: list[Walk], now: datetime) -> int:
    """Espelha calculateCareStreakDays do front."""
    unique_days = {
        _start_of_local_day_ts(dt)
        for dt in (_parse_walk_datetime(walk) for walk in completed_walks)
        if dt is not None
    }
    if not unique_days:
        return 0

    today = _start_of_local_day_ts(now)
    yesterday = today - ONE_DAY_SECONDS
    latest = max(unique_days)
    if latest < yesterday:
        return 0

    streak = 0
    day = latest
    while day in unique_days:
        streak += 1
        day -= ONE_DAY_SECONDS
    return streak


def get_tutor_level(xp: int) -> dict:
    """Espelha getGamificationLevel(xp, tutorLevels)."""
    safe_xp = max(0, int(xp or 0))
    current = TUTOR_LEVELS[0]
    for item in reversed(TUTOR_LEVELS):
        if safe_xp >= item["minXp"]:
            current = item
            break

    next_level_xp = current["nextLevelXp"]
    if next_level_xp is None:
        xp_to_next_level = None
        progress = 100
    else:
        xp_to_next_level = max(0, next_level_xp - safe_xp)
        rng = next_level_xp - current["minXp"]
        progress = min(100, round(((safe_xp - current["minXp"]) / rng) * 100))

    return {
        "level": current["level"],
        "title": current["title"],
        "nextLevelXp": next_level_xp,
        "xpToNextLevel": xp_to_next_level,
        "progressPercentage": progress,
    }


def build_tutor_badges(
    *,
    scheduled_walks: int,
    total_walks_completed: int,
    care_streak_days: int,
    all_pets_complete: bool,
    xp: int,
    now_iso: str,
) -> list[TutorBadge]:
    """Espelha buildTutorBadges do front."""
    level = get_tutor_level(xp)["level"]
    items = [
        ("tutor-badge-first-care", "first_care", "calendar-outline", "Primeiro cuidado",
         "Primeiro passeio agendado pelo app.", scheduled_walks >= 1),
        ("tutor-badge-dedicated", "dedicated_tutor", "checkmark-circle-outline", "Tutor dedicado",
         "Tres passeios concluidos.", total_walks_completed >= 3),
        ("tutor-badge-week", "care_week", "flame-outline", "Semana de carinho",
         "Sete dias mantendo a sequencia de cuidado.", care_streak_days >= 7),
        ("tutor-badge-family", "complete_family", "paw-outline", "Familia completa",
         "Informacoes importantes dos pets cadastradas.", all_pets_complete),
        ("tutor-badge-premium", "premium_tutor", "ribbon-outline", "Tutor premium",
         "Chegou ao nivel 5 como tutor.", level >= 5),
    ]
    return [
        TutorBadge(
            id=item_id,
            type=badge_type,
            icon=icon,
            name=name,
            description=description,
            status="unlocked" if achieved else "locked",
            unlockedAt=now_iso if achieved else None,
        )
        for item_id, badge_type, icon, name, description, achieved in items
    ]


def get_tutor_gamification(user, db: Session, now: datetime | None = None) -> TutorGamification:
    """Computa a gamificacao do tutor a partir dos seus passeios e pets."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat()

    walks = db.query(Walk).filter(Walk.tutor_id == user.id).all()

    completed = [w for w in walks if is_completed_walk(w) and not is_cancelled_walk(w)]

    # Agendados (upcoming): mesma regra do selectUpcomingWalks do front.
    scheduled = 0
    for walk in walks:
        starts_at = _parse_walk_datetime(walk)
        if not starts_at:
            continue
        status = _walk_status(walk)
        if (
            starts_at.timestamp() >= now.timestamp()
            and status not in INVALID_UPCOMING_STATUSES
            and status not in PENDING_ACCEPTANCE_STATUSES
            and status not in RECOVERY_STATUSES
            and not is_cancelled_walk(walk)
            and not is_active_walk(walk)
        ):
            scheduled += 1

    total_walks_completed = len(completed)
    care_streak_days = calculate_care_streak_days(completed, now)
    xp = total_walks_completed * 45 + scheduled * 10

    total_pets_registered = db.query(Pet).filter(Pet.tutor_id == user.id).count()
    all_pets_complete = total_pets_registered > 0

    # last_care_action_at: mesma escolha do front (primeiro concluido por ordem desc).
    completed_dates = sorted(
        (dt for dt in (_parse_walk_datetime(w) for w in completed) if dt is not None),
        reverse=True,
    )
    last_care_action_at = _to_iso(completed_dates[0]) if completed_dates else None

    level = get_tutor_level(xp)
    badges = build_tutor_badges(
        scheduled_walks=scheduled,
        total_walks_completed=total_walks_completed,
        care_streak_days=care_streak_days,
        all_pets_complete=all_pets_complete,
        xp=xp,
        now_iso=now_iso,
    )

    return TutorGamification(
        tutor_id=str(user.id),
        tutor_xp=xp,
        tutor_level=level["level"],
        tutor_level_title=level["title"],
        next_level_xp=level["nextLevelXp"],
        xp_to_next_level=level["xpToNextLevel"],
        level_progress_percentage=level["progressPercentage"],
        care_streak_days=care_streak_days,
        last_care_action_at=last_care_action_at,
        total_walks_completed=total_walks_completed,
        total_pets_registered=total_pets_registered,
        badges=badges,
        recent_events=[
            GamificationEvent(
                id="tutor-event-streak",
                title="Sequencia de cuidado",
                description=f"{care_streak_days} dias mantendo a rotina do pet.",
                createdAt=now_iso,
            )
        ],
        updated_at=now_iso,
    )
