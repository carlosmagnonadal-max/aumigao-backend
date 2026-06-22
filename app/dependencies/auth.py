import logging
from datetime import datetime, timezone
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.user import User

logger = logging.getLogger(__name__)

# EPIC 4.2 — cutoff de tokens legados sem claim `ver`.
# Tokens sem `ver` cujo `iat` (issued-at) seja ANTERIOR a esta data são rejeitados:
# eles foram emitidos antes da introdução do token_version e não podem mais ser
# validados com segurança (token_version desconhecido → revogação impossível).
# Tokens sem `ver` com `iat` recente (após o cutoff) ainda são aceitos durante a
# janela de transição — retrocompat para testes/tokens de desenvolvimento.
# Data escolhida: 2026-06-01 00:00:00 UTC — antes do deploy do token_version (2026-06-17).
LEGACY_TOKEN_CUTOFF = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

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
    except Exception as _exc:
        logger.warning("token_decode_failed reason=%s", type(_exc).__name__)
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
    # EPIC 4.2 — cutoff de tokens legados: rejeita tokens sem `ver` cujo `iat`
    # (issued-at) seja anterior ao LEGACY_TOKEN_CUTOFF. Isso elimina tokens muito
    # antigos (pré-token_version) para os quais não conseguimos verificar revogação.
    # Tokens sem `ver` com iat recente (após o cutoff) ainda passam — retrocompat.
    if token_ver is None:
        iat = payload.get("iat")
        if iat is not None:
            try:
                iat_dt = datetime.fromtimestamp(int(iat), tz=timezone.utc)
            except Exception:
                iat_dt = None
            if iat_dt is not None and iat_dt < LEGACY_TOKEN_CUTOFF:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token legado expirado. Faca login novamente.")
    # Armazena o tenant alvo (somente super_admin usa; tenant_scope.py filtra por role).
    # O valor é isolado por request — não há estado compartilhado entre requisições.
    user._act_as_tenant_id = x_act_as_tenant or None
    # Publica user_id no ContextVar para que o logging filter o injete em todos os records.
    try:
        from app.core.request_context import user_id_var
        user_id_var.set(str(user.id))
    except Exception:
        pass  # never block auth for logging bookkeeping
    return user

def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in {"admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    return user
