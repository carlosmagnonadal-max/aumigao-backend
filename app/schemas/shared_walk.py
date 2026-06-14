from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


# ----- Config por tenant (admin) -----
class SharedWalkConfigResponse(BaseModel):
    tenant_id: str
    price_per_pet: float
    price_30: float
    price_45: float
    price_60: float
    max_pets_same_tutor: int
    max_tutors: int
    pool_enabled: bool
    pool_radius_km: float
    pool_time_window_min: int
    active: bool


class SharedWalkConfigUpdate(BaseModel):
    price_per_pet: float | None = Field(default=None, ge=0)
    price_30: float | None = Field(default=None, ge=0)
    price_45: float | None = Field(default=None, ge=0)
    price_60: float | None = Field(default=None, ge=0)
    max_pets_same_tutor: int | None = Field(default=None, ge=1, le=3)
    max_tutors: int | None = Field(default=None, ge=2, le=3)
    pool_enabled: bool | None = None
    pool_radius_km: float | None = Field(default=None, gt=0)
    pool_time_window_min: int | None = Field(default=None, ge=0)
    active: bool | None = None


# ----- Sessão / participantes (cliente) -----
class SharedWalkParticipantResponse(ORMModel):
    id: str
    tutor_id: str
    pet_id: str
    role: str
    status: str
    price: float
    pet_name: str | None = None


class SharedWalkResponse(ORMModel):
    id: str
    tenant_id: str
    created_by_tutor_id: str
    status: str
    origin: str
    scheduled_date: str
    duration_minutes: int
    price_per_pet: float
    max_tutors: int
    open_to_pool: bool
    walker_id: str | None = None
    created_at: datetime
    participants: list[SharedWalkParticipantResponse] = Field(default_factory=list)
    # Quantos tutores distintos já no grupo (conveniência para o app).
    tutor_count: int = 0


class SharedWalkCreate(BaseModel):
    scheduled_date: str
    duration_minutes: int = 45
    # Pets do próprio host (1..max_pets_same_tutor). Vários = caso "mesmo tutor".
    host_pet_ids: list[str] = Field(min_length=1)
    open_to_pool: bool = False


class SharedWalkJoin(BaseModel):
    pet_id: str


class SharedWalkView(BaseModel):
    """Resposta cliente-final: disponibilidade (flag) + config + minhas sessões."""

    available: bool
    price_per_pet: float | None = None
    price_30: float | None = None
    price_45: float | None = None
    price_60: float | None = None
    max_tutors: int | None = None
    max_pets_same_tutor: int | None = None
    pool_enabled: bool = False
    sessions: list[SharedWalkResponse] = Field(default_factory=list)
