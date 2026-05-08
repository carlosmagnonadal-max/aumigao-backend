from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr
from app.schemas.common import ORMModel

class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str = ""
    role: str = "tutor"
    referral_code: str | None = None
    cpf: str | None = None
    phone: str | None = None
    profile: dict[str, Any] | None = None

class UserResponse(ORMModel):
    id: str
    email: EmailStr
    full_name: str = ""
    role: str
    is_active: bool
    created_at: datetime
