from pydantic import BaseModel, Field


class PetTourConfigResponse(BaseModel):
    tenant_id: str
    base_price: float
    min_duration_minutes: int
    active: bool


class PetTourConfigUpdate(BaseModel):
    base_price: float | None = Field(default=None, ge=0)
    min_duration_minutes: int | None = Field(default=None, ge=61)
    active: bool | None = None


class PetTourView(BaseModel):
    """Resposta cliente-final: disponibilidade (feature flag) + config do tenant."""

    available: bool
    base_price: float | None = None
    min_duration_minutes: int | None = None
