from datetime import datetime
from pydantic import BaseModel, Field
from app.schemas.common import ORMModel

class WalkCreate(BaseModel):
    pet_id: str
    walker_id: str | None = None
    walker_selection_mode: str | None = None
    scheduled_date: str
    duration_minutes: int
    price: float
    pickup_method: str = "Buscar em casa"
    address_snapshot: str = ""
    notes: str = ""

class WalkUpdateStatus(BaseModel):
    status: str

class WalkResponse(ORMModel):
    id: str
    tutor_id: str
    walker_id: str | None = None
    pet_id: str
    pet_name: str | None = None
    pet_photo_url: str | None = None
    tutor_name: str | None = None
    client_name: str | None = None
    walker_name: str | None = None
    scheduled_date: str
    walk_date: str | None = None
    walk_time: str | None = None
    duration_minutes: int
    price: float
    status: str
    operational_status: str | None = None
    operationalStatus: str | None = None
    walker_selection_mode: str | None = None
    walkerSelectionMode: str | None = None
    assigned_walker_id: str | None = None
    assignedWalkerId: str | None = None
    current_attempt: int | None = None
    current_matching_attempt: int | None = None
    max_attempts: int | None = None
    max_matching_attempts: int | None = None
    confirmation_expires_at: datetime | None = None
    walker_confirmation_expires_at: datetime | None = None
    matching_started_at: datetime | None = None
    matching_finished_at: datetime | None = None
    no_walker_reason: str | None = None
    pickup_region_label: str | None = None
    pickup_distance_label: str | None = None
    pickup_privacy_level: str | None = None
    matching_attempts: list[dict] = Field(default_factory=list)
    operational_logs: list[dict] = Field(default_factory=list)
    pickup_method: str
    address_snapshot: str
    notes: str
    created_at: datetime
