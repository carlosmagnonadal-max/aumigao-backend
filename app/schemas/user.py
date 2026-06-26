from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field
from app.schemas.common import ORMModel

class UserCreate(BaseModel):
    # Sec-P3: max_length defensivos — anti-DoS/log-injection. Limites generosos,
    # não quebram uso legítimo.
    # Sec-fix: EmailStr valida formato antes de qualquer processamento de rota.
    email: EmailStr = Field(..., max_length=254)
    # Sec-fix: min_length=8 rejeita senhas trivialmente curtas na camada de schema
    # (defense-in-depth; a rota já valida força, mas isso bloqueia mais cedo).
    password: str = Field(..., min_length=8, max_length=128)
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
    # Plumbing tenant_id: permite que o admin-web saiba qual tenant o usuário pertence
    # (necessário para tenant-admin ver o próprio extrato de provisão fiscal).
    tenant_id: str | None = None
