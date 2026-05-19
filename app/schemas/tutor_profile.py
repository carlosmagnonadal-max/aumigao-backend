from datetime import datetime
from pydantic import BaseModel
from app.schemas.common import ORMModel

class TutorProfileBase(BaseModel):
    full_name: str = ""
    cpf: str = ""
    phone: str = ""
    photo_url: str | None = None
    cep: str = ""
    street: str = ""
    number: str = ""
    complement: str = ""
    neighborhood: str = ""
    city: str = ""
    state: str = ""
    reference_point: str = ""
    access_instructions: str = ""
    pickup_notes: str | None = ""
    preferred_method: str = "Buscar em casa"

class TutorProfileCreate(TutorProfileBase):
    pass
class TutorProfileUpdate(TutorProfileBase):
    pass
class TutorProfileResponse(TutorProfileBase, ORMModel):
    id: str
    user_id: str
    created_at: datetime
