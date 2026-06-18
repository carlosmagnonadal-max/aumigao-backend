from datetime import datetime
from pydantic import BaseModel, Field
from app.schemas.common import ORMModel

class PetBase(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection. Limites generosos.
    name: str = Field(..., max_length=200)
    photo_url: str | None = Field(None, max_length=2000)
    species: str = Field("Cachorro", max_length=100)
    sex: str = Field("", max_length=50)
    breed: str = Field("", max_length=200)
    size: str = Field("", max_length=50)
    weight: float | None = None
    age: int | None = None
    behavior_notes: str = Field("", max_length=2000)
    is_social: bool = True
    afraid_of_noise: bool = False
    pulls_leash: bool = False
    can_walk_with_other_pets: bool = False
    is_neutered: bool | None = False
    allergies: str = Field("", max_length=2000)
    medications: str = Field("", max_length=2000)
    restrictions: str = Field("", max_length=2000)
    health_notes: str = Field("", max_length=2000)

class PetCreate(PetBase):
    pass
class PetUpdate(PetBase):
    name: str | None = None
class PetResponse(PetBase, ORMModel):
    id: str
    tutor_id: str
    created_at: datetime
