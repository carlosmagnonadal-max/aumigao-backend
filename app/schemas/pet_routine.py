"""Schemas da rotina/evolucao do pet (compute-based, sem tabela).

Espelha o contrato PetRoutine ja definido no front
(frontend/types/gamification.ts). Sao campos calculados a partir dos passeios
concluidos do pet — nenhuma coluna/migracao nova.
"""
from pydantic import BaseModel


class PetBadgeView(BaseModel):
    id: str
    type: str
    icon: str
    name: str
    description: str
    status: str  # "locked" | "unlocked"
    unlockedAt: str | None = None


class PetEvolutionEventView(BaseModel):
    id: str
    title: str
    description: str
    createdAt: str


class PetRoutineView(BaseModel):
    """Resposta de GET /pets/{pet_id}/routine — espelha o tipo PetRoutine do front."""

    pet_id: str
    tutor_id: str
    name: str
    breed: str | None = None
    age: int | None = None
    size: str | None = None
    energy_profile: str  # "low" | "medium" | "high"
    last_walk_at: str | None = None
    weekly_walk_count: int
    xp: int
    level: int
    current_status: str  # PetRoutineStatus
    status_label: str
    message: str
    cta_label: str
    cta_href: str
    routine_progress_percentage: int
    level_title: str
    next_level_xp: int | None = None
    xp_to_next_level: int | None = None
    level_progress_percentage: int
    next_badge: PetBadgeView | None = None
    badges: list[PetBadgeView]
    history: list[PetEvolutionEventView]
    created_at: str
    updated_at: str
