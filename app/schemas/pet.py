from datetime import date, datetime
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
    # Dieta estruturada (Perfil Vivo 2.0 — Fase A). Editável tb via PATCH /profile
    # (PetHealthUpdate). Exposta aqui para o app poder ler/gravar no CRUD do pet.
    diet_type: str | None = Field(None, max_length=50)  # seca|umida|natural|mista|outro
    diet_brand: str | None = Field(None, max_length=200)
    diet_line: str | None = Field(None, max_length=200)
    diet_grams_per_meal: int | None = Field(None, ge=0)
    diet_meals_per_day: int | None = Field(None, ge=0)
    diet_meal_times: str | None = Field(None, max_length=500)  # JSON simples (lista de horários)
    diet_notes: str | None = Field(None, max_length=2000)
    # Ficha expandida (Perfil Vivo P0 — 0094). Todas opcionais.
    # supplements_json/fear_triggers_json = JSON simples em str (padrão diet_meal_times).
    supplements_json: str | None = Field(None, max_length=4000)  # JSON: [{name,dose,frequency}]
    food_bag_weight_kg: float | None = Field(None, ge=0)
    food_bag_opened_at: date | None = None
    vet_clinic: str | None = Field(None, max_length=200)
    insurance_provider: str | None = Field(None, max_length=200)
    insurance_policy: str | None = Field(None, max_length=200)
    behavior_with_dogs: str | None = Field(None, max_length=50)  # amigavel|indiferente|reativo|desconhecido
    behavior_with_children: str | None = Field(None, max_length=50)
    behavior_with_cats: str | None = Field(None, max_length=50)
    fear_triggers_json: str | None = Field(None, max_length=2000)  # JSON: ["trovão","fogos",...]

class PetCreate(PetBase):
    pass
class PetUpdate(PetBase):
    name: str | None = None
class PetResponse(PetBase, ORMModel):
    id: str
    tutor_id: str
    created_at: datetime
