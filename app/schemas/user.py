from datetime import datetime
from pydantic import BaseModel, EmailStr
from app.schemas.common import ORMModel

class UserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: str = ""
    role: str = "tutor"

class UserResponse(ORMModel):
    id: str
    email: EmailStr
    full_name: str = ""
    role: str
    is_active: bool
    created_at: datetime
