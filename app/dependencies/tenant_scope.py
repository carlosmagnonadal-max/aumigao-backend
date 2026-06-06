from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from app.models.user import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AdminTenantScope:
    user: User
    tenant_id: str | None
    is_global: bool
    role: str


def is_super_admin(user: User) -> bool:
    return getattr(user, "role", None) == "super_admin"


def _log_super_admin_bypass_once(user: User) -> None:
    if getattr(user, "_tenant_scope_super_admin_bypass_logged", False):
        return

    logger.warning(
        "super_admin_global_tenant_bypass",
        extra={
            "user_id": getattr(user, "id", None),
            "role": getattr(user, "role", None),
        },
    )
    try:
        setattr(user, "_tenant_scope_super_admin_bypass_logged", True)
    except Exception:
        pass


def get_admin_tenant_scope(user: User) -> AdminTenantScope:
    role = getattr(user, "role", "")

    if is_super_admin(user):
        _log_super_admin_bypass_once(user)
        return AdminTenantScope(
            user=user,
            tenant_id=None,
            is_global=True,
            role=role,
        )

    if role != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")

    tenant_id = getattr(user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Admin sem tenant_id configurado")

    return AdminTenantScope(
        user=user,
        tenant_id=tenant_id,
        is_global=False,
        role=role,
    )


def ensure_tenant_access(obj_tenant_id: str | None, scope: AdminTenantScope) -> None:
    if scope.is_global:
        return

    if obj_tenant_id and obj_tenant_id == scope.tenant_id:
        return

    raise HTTPException(status_code=404, detail="Recurso nao encontrado")


def apply_tenant_filter(
    query: Any,
    model: Any,
    scope: AdminTenantScope,
    tenant_column: Any | None = None,
):
    if scope.is_global:
        return query

    column = tenant_column
    if column is None:
        column = getattr(model, "tenant_id", None)

    if column is None:
        model_name = getattr(model, "__name__", model.__class__.__name__)
        raise ValueError(
            f"{model_name} nao possui tenant_id; informe tenant_column explicitamente."
        )

    return query.filter(column == scope.tenant_id)
