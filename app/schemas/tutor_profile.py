from datetime import datetime
from pydantic import BaseModel, Field
from app.schemas.common import ORMModel

class TutorProfileBase(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection. Limites generosos.
    full_name: str = Field("", max_length=200)
    cpf: str = Field("", max_length=20)
    phone: str = Field("", max_length=30)
    photo_url: str | None = Field(None, max_length=2000)
    cep: str = Field("", max_length=20)
    street: str = Field("", max_length=300)
    number: str = Field("", max_length=30)
    complement: str = Field("", max_length=200)
    neighborhood: str = Field("", max_length=200)
    city: str = Field("", max_length=200)
    state: str = Field("", max_length=100)
    reference_point: str = Field("", max_length=500)
    access_instructions: str = Field("", max_length=2000)
    pickup_notes: str | None = Field("", max_length=2000)
    preferred_method: str = Field("Buscar em casa", max_length=100)

class TutorProfileCreate(TutorProfileBase):
    pass
class TutorProfileUpdate(TutorProfileBase):
    pass
class TutorProfileResponse(TutorProfileBase, ORMModel):
    id: str
    user_id: str
    created_at: datetime
