"""RBAC/ABAC — checagem de permissões (Sprint 15, passo 3).

Convive com `require_admin` durante a migração das rotas. A autorização final é
sempre server-side: `user_has_permission` percorre
user_role_assignments -> role_permissions -> permissions.
"""
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.rbac import Permission, RolePermission, UserRoleAssignment
from app.models.user import User


def user_has_permission(db: Session, user: User, permission_key: str) -> bool:
    """True se o usuário possui a permissão via algum papel atribuído (ativo).

    Rede de segurança durante a transição: super_admin (role string) sempre passa,
    mesmo que o seed de papéis ainda não o cubra. Removível no passo 4.
    """
    if getattr(user, "role", None) == "super_admin":
        return True
    found = (
        db.query(UserRoleAssignment.id)
        .join(RolePermission, RolePermission.role_id == UserRoleAssignment.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .filter(
            UserRoleAssignment.user_id == user.id,
            UserRoleAssignment.revoked_at.is_(None),
            Permission.key == permission_key,
        )
        .first()
    )
    return found is not None


def require_permission(permission_key: str):
    """Dependency factory: exige que o usuário autenticado tenha `permission_key`.

    Retorna o `User` (para reuso em filtros/escopo na rota).
    """

    def _dependency(
        user: User = Depends(get_current_user), db: Session = Depends(get_db)
    ) -> User:
        if not user_has_permission(db, user, permission_key):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Permissao negada"
            )
        return user

    return _dependency
