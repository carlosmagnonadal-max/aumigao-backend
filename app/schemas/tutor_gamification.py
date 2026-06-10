"""Schemas da gamificacao do TUTOR (compute-based, sem tabela).

Espelha EXATAMENTE o contrato do front em frontend/types/gamification.ts:
- TutorBadge
- PetEvolutionEvent (reaproveitado em recent_events)
- TutorGamification
"""
from typing import Literal

from pydantic import BaseModel

TutorBadgeType = Literal[
    "first_care",
    "dedicated_tutor",
    "care_week",
    "complete_family",
    "premium_tutor",
]

GamificationBadgeStatus = Literal["locked", "unlocked"]


class TutorBadge(BaseModel):
    id: str
    type: TutorBadgeType
    icon: str
    name: str
    description: str
    status: GamificationBadgeStatus
    unlockedAt: str | None = None


class GamificationEvent(BaseModel):
    """Espelha PetEvolutionEvent do front (usado em recent_events)."""

    id: str
    title: str
    description: str
    createdAt: str


class TutorGamification(BaseModel):
    tutor_id: str
    tutor_xp: int
    tutor_level: int
    tutor_level_title: str
    next_level_xp: int | None = None
    xp_to_next_level: int | None = None
    level_progress_percentage: int
    care_streak_days: int
    last_care_action_at: str | None = None
    total_walks_completed: int
    total_pets_registered: int
    badges: list[TutorBadge]
    recent_events: list[GamificationEvent]
    updated_at: str
