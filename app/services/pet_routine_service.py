"""Servico da rotina/evolucao do pet — espelho EXATO da logica do front.

Referencia de contrato (NAO divergir):
- frontend/types/gamification.ts (tipo PetRoutine)
- frontend/utils/gamification.ts (calculatePetStatus, getPetRoutineCopy,
  getGamificationLevel, buildPetBadges, buildPetRoutine)

Aqui os inputs do front (weeklyWalkCount, totalWalksCompleted, xp, lastWalkAt,
previousStatus, routineStreakDays) sao COMPUTADOS a partir dos passeios
concluidos do pet — sem nenhuma tabela/coluna nova.

Regras (do contrato):
- current_status por horas desde o ultimo passeio concluido:
  <= 6 post_walk_satisfied | <= 24 rested | > 48 very_active | demais needs_energy
  (sem ultimo passeio => undefined)
- xp = totalWalksCompleted * 35 + weeklyWalkCount * 10
- niveis do pet (minXp): 0 / 100 / 250 / 500 / 900
- meta semanal = 3 passeios (routine_progress_percentage)
- badges: first_walk, active_week, perfect_routine, energy_controlled,
  healthy_pet, elite_pet
"""
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.pet import Pet
from app.models.walk import Walk
from app.services.weekly_mission_service import (
    get_current_week_range,
    parse_walk_datetime,
)

# Passeios que contam como concluidos (mesma definicao usada em routes/walks.py).
COMPLETED_WALK_STATUSES = {
    "Finalizado",
    "Concluido",
    "Concluído",
    "finalizado",
    "completed",
    "finished",
}

HOUR = 3600  # segundos

# Espelha petLevels do front (level, title, minXp, nextLevelXp).
PET_LEVELS = [
    {"level": 1, "title": "Comecando a rotina", "minXp": 0, "nextLevelXp": 100},
    {"level": 2, "title": "Pet ativo", "minXp": 100, "nextLevelXp": 250},
    {"level": 3, "title": "Pet saudavel", "minXp": 250, "nextLevelXp": 500},
    {"level": 4, "title": "Pet atleta", "minXp": 500, "nextLevelXp": 900},
    {"level": 5, "title": "Pet elite", "minXp": 900, "nextLevelXp": None},
]

WEEKLY_TARGET = 3
XP_PER_WALK = 35
XP_PER_WEEKLY_WALK = 10


def calculate_pet_status(last_walk_at: datetime | None, now: datetime) -> str:
    """Espelho de calculatePetStatus (front)."""
    if not last_walk_at:
        return "undefined"
    elapsed_hours = (now - last_walk_at).total_seconds() / HOUR
    if elapsed_hours <= 6:
        return "post_walk_satisfied"
    if elapsed_hours <= 24:
        return "rested"
    if elapsed_hours > 48:
        return "very_active"
    return "needs_energy"


def get_pet_routine_copy(name: str, status: str) -> dict:
    """Espelho de getPetRoutineCopy (front)."""
    pet_name = name or "Seu pet"
    copy = {
        "post_walk_satisfied": {
            "label": "Satisfeito pos-passeio",
            "message": f"{pet_name} voltou feliz do ultimo passeio. Agora e hora de descanso e carinho.",
            "cta": "Ver resumo do passeio",
            "href": "/(tabs)/passeios",
        },
        "rested": {
            "label": "Descansado",
            "message": f"{pet_name} esta bem hoje. Manter a rotina ajuda na saude e no comportamento.",
            "cta": "Agendar proximo passeio",
            "href": "/agendar",
        },
        "needs_energy": {
            "label": "Precisa gastar energia",
            "message": f"{pet_name} esta ha um tempo sem passear. Um passeio hoje pode ajudar a gastar energia.",
            "cta": "Agendar passeio",
            "href": "/agendar",
        },
        "very_active": {
            "label": "Muito ativo",
            "message": f"{pet_name} pode estar acumulando energia. Recomendamos um passeio para ajudar no equilibrio.",
            "cta": "Agendar agora",
            "href": "/agendar",
        },
        "undefined": {
            "label": "Rotina ainda nao definida",
            "message": f"Vamos comecar a entender a rotina do {pet_name} para cuidar melhor dele.",
            "cta": "Criar primeira rotina",
            "href": "/agendar",
        },
    }
    return copy[status]


def get_gamification_level(xp: int) -> dict:
    """Espelho de getGamificationLevel (front) para os niveis do pet."""
    safe_xp = max(0, int(xp or 0))
    current = PET_LEVELS[0]
    for item in reversed(PET_LEVELS):
        if safe_xp >= item["minXp"]:
            current = item
            break
    next_level_xp = current["nextLevelXp"]
    if next_level_xp is None:
        xp_to_next = None
        progress = 100
    else:
        xp_to_next = max(0, next_level_xp - safe_xp)
        range_ = next_level_xp - current["minXp"]
        progress = min(100, round(((safe_xp - current["minXp"]) / range_) * 100)) if range_ else 100
    return {
        "level": current["level"],
        "title": current["title"],
        "minXp": current["minXp"],
        "nextLevelXp": next_level_xp,
        "progressPercentage": progress,
        "xpToNextLevel": xp_to_next,
    }


def build_pet_badges(
    *,
    weekly_walk_count: int,
    total_walks_completed: int,
    routine_streak_days: int,
    previous_status: str | None,
    current_status: str,
    xp: int,
    now_iso: str,
) -> list[dict]:
    """Espelho de buildPetBadges (front)."""
    level = get_gamification_level(xp)["level"]
    unlocked = {
        "first_walk": total_walks_completed >= 1,
        "active_week": weekly_walk_count >= 3,
        "perfect_routine": routine_streak_days >= 7,
        "energy_controlled": previous_status == "very_active"
        and current_status == "post_walk_satisfied",
        "healthy_pet": level >= 3,
        "elite_pet": level >= 5,
    }
    items = [
        ("pet-badge-first-walk", "first_walk", "footsteps-outline", "Primeiro passeio",
         "Primeiro passeio finalizado pelo pet."),
        ("pet-badge-active-week", "active_week", "calendar-outline", "Semana ativa",
         "Tres passeios concluidos na mesma semana."),
        ("pet-badge-perfect-routine", "perfect_routine", "sparkles-outline", "Rotina perfeita",
         "Sete dias mantendo uma rotina de cuidado."),
        ("pet-badge-energy", "energy_controlled", "fitness-outline", "Energia controlada",
         "Saiu de muito ativo para satisfeito pos-passeio."),
        ("pet-badge-healthy", "healthy_pet", "heart-outline", "Pet saudavel",
         "Chegou ao nivel 3 de evolucao."),
        ("pet-badge-elite", "elite_pet", "ribbon-outline", "Pet elite",
         "Chegou ao nivel 5 de evolucao."),
    ]
    badges = []
    for badge_id, badge_type, icon, name, description in items:
        achieved = unlocked[badge_type]
        badges.append({
            "id": badge_id,
            "type": badge_type,
            "icon": icon,
            "name": name,
            "description": description,
            "status": "unlocked" if achieved else "locked",
            "unlockedAt": now_iso if achieved else None,
        })
    return badges


def _completed_walks(pet: Pet, db: Session) -> list[tuple[datetime, Walk]]:
    """Passeios concluidos do pet com data resolvida, ordenados do mais antigo ao mais novo."""
    walks = db.query(Walk).filter(Walk.pet_id == pet.id).all()
    result: list[tuple[datetime, Walk]] = []
    for walk in walks:
        if walk.status not in COMPLETED_WALK_STATUSES:
            continue
        when = parse_walk_datetime(walk.scheduled_date) or walk.created_at
        if when is None:
            continue
        result.append((when, walk))
    result.sort(key=lambda item: item[0])
    return result


def _routine_streak_days(completed_dates: list[datetime], now: datetime) -> int:
    """Dias consecutivos (a partir do mais recente) com ao menos um passeio concluido.

    Conta o periodo continuo de dias-calendario terminando no dia do ultimo
    passeio, sem buracos de mais de 1 dia. So conta a sequencia atual se o ultimo
    passeio foi hoje ou ontem (senao a rotina foi interrompida).
    """
    if not completed_dates:
        return 0
    days = sorted({d.date() for d in completed_dates}, reverse=True)
    today = now.date()
    if (today - days[0]).days > 1:
        return 0
    streak = 1
    for prev, curr in zip(days, days[1:]):
        if (prev - curr).days == 1:
            streak += 1
        else:
            break
    return streak


def build_pet_routine(pet: Pet, db: Session, now: datetime | None = None) -> dict:
    """Monta o PetRoutine do pet computando tudo a partir dos passeios concluidos.

    Espelha buildPetRoutine do front.
    """
    now = now or datetime.utcnow()
    now_iso = now.isoformat()

    completed = _completed_walks(pet, db)
    completed_dates = [when for when, _ in completed]
    total_walks_completed = len(completed)

    last_walk_at = completed_dates[-1] if completed_dates else None
    last_walk_iso = last_walk_at.isoformat() if last_walk_at else None

    week_start, week_end = get_current_week_range(now)
    weekly_walk_count = sum(1 for when in completed_dates if week_start <= when <= week_end)

    xp = total_walks_completed * XP_PER_WALK + weekly_walk_count * XP_PER_WEEKLY_WALK

    current_status = calculate_pet_status(last_walk_at, now)
    # previous_status: status calculado a partir do penultimo passeio concluido,
    # avaliado no momento do ULTIMO passeio (para a badge energy_controlled).
    previous_status: str | None = None
    if len(completed_dates) >= 2 and last_walk_at is not None:
        previous_status = calculate_pet_status(completed_dates[-2], last_walk_at)

    routine_streak_days = _routine_streak_days(completed_dates, now)

    copy = get_pet_routine_copy(pet.name, current_status)
    level = get_gamification_level(xp)
    routine_progress = min(100, round((min(weekly_walk_count, WEEKLY_TARGET) / WEEKLY_TARGET) * 100))

    badges = build_pet_badges(
        weekly_walk_count=weekly_walk_count,
        total_walks_completed=total_walks_completed,
        routine_streak_days=routine_streak_days,
        previous_status=previous_status,
        current_status=current_status,
        xp=xp,
        now_iso=now_iso,
    )
    next_badge = next((b for b in badges if b["status"] == "locked"), None)

    created_at = pet.created_at.isoformat() if pet.created_at else now_iso

    return {
        "pet_id": pet.id,
        "tutor_id": pet.tutor_id,
        "name": pet.name,
        "breed": pet.breed or None,
        "age": pet.age,
        "size": pet.size or None,
        "energy_profile": "medium",
        "last_walk_at": last_walk_iso,
        "weekly_walk_count": weekly_walk_count,
        "xp": xp,
        "level": level["level"],
        "current_status": current_status,
        "status_label": copy["label"],
        "message": copy["message"],
        "cta_label": copy["cta"],
        "cta_href": copy["href"],
        "routine_progress_percentage": routine_progress,
        "level_title": level["title"],
        "next_level_xp": level["nextLevelXp"],
        "xp_to_next_level": level["xpToNextLevel"],
        "level_progress_percentage": level["progressPercentage"],
        "next_badge": next_badge,
        "badges": badges,
        "history": [
            {
                "id": "pet-event-week",
                "title": "Rotina da semana",
                "description": f"{weekly_walk_count} de {WEEKLY_TARGET} passeios recomendados concluidos.",
                "createdAt": now_iso,
            },
            {
                "id": "pet-event-level",
                "title": f"Nivel {level['level']}",
                "description": f"{pet.name} esta em {level['title']}.",
                "createdAt": now_iso,
            },
        ],
        "created_at": created_at,
        "updated_at": now_iso,
    }
