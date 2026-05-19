from datetime import datetime
from pydantic import BaseModel
from app.schemas.common import ORMModel

class PetBase(BaseModel):
    name: str
    photo_url: str | None = None
    species: str = "Cachorro"
    sex: str = ""
    breed: str = ""
    size: str = ""
    weight: float | None = None
    age: int | None = None
    behavior_notes: str = ""
    is_social: bool = True
    afraid_of_noise: bool = False
    pulls_leash: bool = False
    can_walk_with_other_pets: bool = False
    is_neutered: bool | None = False
    allergies: str = ""
    medications: str = ""
    restrictions: str = ""
    health_notes: str = ""

class PetCreate(PetBase):
    pass
class PetUpdate(PetBase):
    name: str | None = None
class PetResponse(PetBase, ORMModel):
    id: str
    tutor_id: str
    created_at: datetime
