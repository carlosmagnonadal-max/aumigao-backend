from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from app.schemas.common import ORMModel

# Wave 5 — valores aceitos de porte máximo de cão (contrato fixo PT).
_VALID_MAX_DOG_SIZES = {"Pequeno", "Médio", "Grande"}

class WalkerProfileBase(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection. Limites generosos.
    full_name: str = Field("", max_length=200)
    cpf: str = Field("", max_length=20)
    phone: str = Field("", max_length=30)
    birth_date: str = Field("", max_length=30)
    city: str = Field("", max_length=200)
    state: str = Field("", max_length=100)
    experience: str = Field("", max_length=2000)
    bio: str = Field("", max_length=2000)
    rg: str = Field("", max_length=30)
    document_url: str | None = Field(None, max_length=2000)
    identity_document_front_url: str | None = Field(None, max_length=2000)
    identity_document_back_url: str | None = Field(None, max_length=2000)
    selfie_url: str | None = Field(None, max_length=2000)
    # Foto opcional com o pet — separada da selfie do documento (obrigatoria).
    pet_photo_url: str | None = Field(None, max_length=2000)
    proof_of_address_url: str | None = Field(None, max_length=2000)
    profile_photo_url: str | None = Field(None, max_length=2000)
    has_vehicle: bool = False
    # Wave 5 — porte máximo de cão aceito. Default permissivo "Grande".
    max_dog_size: str = Field("Grande", max_length=50)

    @field_validator("max_dog_size")
    @classmethod
    def _validate_max_dog_size(cls, value: str) -> str:
        if value not in _VALID_MAX_DOG_SIZES:
            raise ValueError("max_dog_size deve ser 'Pequeno', 'Médio' ou 'Grande'")
        return value

class WalkerProfileCreate(WalkerProfileBase):
    pass
class WalkerProfileUpdate(WalkerProfileBase):
    pass
class WalkerProfileResponse(WalkerProfileBase, ORMModel):
    id: str
    user_id: str
    status: str
    internal_notes: str = ""
    active_as_walker: bool = False
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    reviewed_by_admin_id: str | None = None
    resubmission_requested_documents: str = ""
    rating_avg: float = 0
    rating_count: int = 0
    recent_review_comments: list[dict] = Field(default_factory=list)
    top_review_tags: list[dict] = Field(default_factory=list)
    operational_score: int = 0
    reliability_label: str = "Em formação"
    score_factors: dict = Field(default_factory=dict)
    score_details: dict = Field(default_factory=dict)
    score_policy: str = ""
