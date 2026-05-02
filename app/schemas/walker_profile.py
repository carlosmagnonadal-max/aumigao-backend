from datetime import datetime
from pydantic import BaseModel
from app.schemas.common import ORMModel

class WalkerProfileBase(BaseModel):
    full_name: str = ""
    phone: str = ""
    birth_date: str = ""
    city: str = ""
    state: str = ""
    experience: str = ""
    bio: str = ""
    rg: str = ""
    document_url: str | None = None
    selfie_url: str | None = None
    proof_of_address_url: str | None = None

class WalkerProfileCreate(WalkerProfileBase):
    pass
class WalkerProfileUpdate(WalkerProfileBase):
    pass
class WalkerProfileResponse(WalkerProfileBase, ORMModel):
    id: str
    user_id: str
    status: str
    rejection_reason: str | None = None
    created_at: datetime
