from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from app.models.user import User

logger = logging.getLogger(__name__)

# Import lazy para evitar ciclo (database → tenant_scope → database).
# set_session_tenant é chamado apenas em runtime, nunca em import-time.
def _set_session_tenant(db, tenant: str) -> None:
    """Wrapper de import tardio para set_session_tenant (evita ciclo circular)."""
    if db is None:
        return
    from app.core.database import set_session_tenant  # noqa: PLC0415
    set_session_tenant(db, tenant)


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


def get_admin_tenant_scope(user: User, db=None) -> AdminTenantScope:
    """Resolve o escopo de tenant do admin e injeta o GUC RLS na sessão.

    db é opcional: quando fornecido, set_session_tenant é chamado para garantir
    que a policy RLS do PostgreSQL receba o valor correto ANTES de qualquer query.
    Callers que não passam db mantêm o comportamento anterior (sem RLS injection).
    """
    role = getattr(user, "role", "")

    if is_super_admin(user):
        act = getattr(user, "_act_as_tenant_id", None)
        if act and isinstance(act, str) and act.strip():
            # super_admin optou por "operar como tenant" — escopo restrito ao tenant escolhido.
            # Não validamos a existência do tenant no banco: se for inválido os filtros
            # retornam vazio, sem vazar dados de outros tenants.
            logger.info(
                "super_admin_act_as_tenant",
                extra={
                    "user_id": getattr(user, "id", None),
                    "act_as_tenant_id": act,
                },
            )
            scope = AdminTenantScope(
                user=user,
                tenant_id=act.strip(),
                is_global=False,
                role=role,
            )
            # Fase 2b: injeta tenant específico (act-as) no GUC RLS da sessão.
            _set_session_tenant(db, act.strip())
            return scope
        # Sem header — comportamento global padrão
        _log_super_admin_bypass_once(user)
        scope = AdminTenantScope(
            user=user,
            tenant_id=None,
            is_global=True,
            role=role,
        )
        # Fase 2b: super_admin global vê tudo — sentinel "*".
        _set_session_tenant(db, "*")
        return scope

    # Admin de tenant: NUNCA permite personificação via header.
    # O _act_as_tenant_id é completamente ignorado aqui — segurança à prova de bala.
    if role != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")

    tenant_id = getattr(user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Admin sem tenant_id configurado")

    scope = AdminTenantScope(
        user=user,
        tenant_id=tenant_id,
        is_global=False,
        role=role,
    )
    # Fase 2b: admin de tenant — restringe RLS ao tenant_id do usuário.
    _set_session_tenant(db, tenant_id)
    return scope


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


def require_tenant(request: Request) -> str:
    """Dependency: exige um tenant resolvido na requisição (spec §6.4).

    No modo estrito (STRICT_TENANT_RESOLUTION) o resolver não faz fallback para o
    tenant padrão; rotas sensíveis usam esta dependency para receber 400
    TENANT_REQUIRED em vez de operar silenciosamente sobre o tenant errado.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="TENANT_REQUIRED")
    return tenant_id
