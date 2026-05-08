from datetime import datetime
from pydantic import BaseModel
from app.schemas.common import ORMModel

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
