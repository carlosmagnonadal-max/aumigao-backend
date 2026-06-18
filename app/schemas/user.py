from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field
from app.schemas.common import ORMModel

class UserCreate(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection. Limites generosos,
    # não quebram uso legítimo.
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=128)
    full_name: str = Field("", max_length=200)
    role: str = Field("tutor", max_length=50)
    referral_code: str | None = Field(None, max_length=100)
    cpf: str | None = Field(None, max_length=20)
    phone: str | None = Field(None, max_length=30)
    profile: dict[str, Any] | None = None

class UserResponse(ORMModel):
    id: str
    email: EmailStr
    full_name: str = ""
    role: str
    is_active: bool
    # B2: flag de troca obrigatoria de senha no 1o login; exposta no login para o
    # admin-web decidir o redirect sem bloquear o token. Default False: usuarios
    # legados (sem coluna no DB ainda) e objetos criados sem persistir veem False.
    must_change_password: bool | None = False
    created_at: datetime
