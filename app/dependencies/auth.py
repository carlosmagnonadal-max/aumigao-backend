from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User

security = HTTPBearer(auto_error=False)

def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
    x_act_as_tenant: str | None = Header(default=None, alias="X-Act-As-Tenant"),
) -> User:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nao autenticado")
    try:
        # B-ALT-011 (passo 2a): valida assinatura+exp+iss+aud de forma retrocompatível
        # (tokens legados sem iss/aud ainda passam; iss/aud errados são rejeitados).
        payload = decode_access_token(credentials.credentials)
        user_id = payload.get("sub")
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token invalido")
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario invalido")
    # B-ALT-011 (passo 2b): revogação de sessão. Se o token traz "ver" e ele ficou para
    # trás do token_version atual (troca/reset de senha bumpou), o token está revogado.
    # Tokens legados sem "ver" são aceitos durante a janela de transição (retrocompat).
    token_ver = payload.get("ver")
    if token_ver is not None and token_ver != (user.token_version or 0):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessao expirada")
    # Armazena o tenant alvo (somente super_admin usa; tenant_scope.py filtra por role).
    # O valor é isolado por request — não há estado compartilhado entre requisições.
    user._act_as_tenant_id = x_act_as_tenant or None
    return user

def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    return user
