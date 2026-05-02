from datetime import datetime
from pydantic import BaseModel
from app.schemas.common import ORMModel

class WalkCreate(BaseModel):
    pet_id: str
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
    scheduled_date: str
    duration_minutes: int
    price: float
    status: str
    pickup_method: str
    address_snapshot: str
    notes: str
    created_at: datetime
