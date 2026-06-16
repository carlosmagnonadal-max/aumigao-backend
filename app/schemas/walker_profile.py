from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from app.schemas.common import ORMModel

# Wave 5 — valores aceitos de porte máximo de cão (contrato fixo PT).
_VALID_MAX_DOG_SIZES = {"Pequeno", "Médio", "Grande"}

class WalkerProfileBase(BaseModel):
    full_name: str = ""
    cpf: str = ""
    phone: str = ""
    birth_date: str = ""
    city: str = ""
    state: str = ""
    experience: str = ""
    bio: str = ""
    rg: str = ""
    document_url: str | None = None
    identity_document_front_url: str | None = None
    identity_document_back_url: str | None = None
    selfie_url: str | None = None
    proof_of_address_url: str | None = None
    profile_photo_url: str | None = None
    has_vehicle: bool = False
    # Wave 5 — porte máximo de cão aceito. Default permissivo "Grande".
    max_dog_size: str = "Grande"

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
