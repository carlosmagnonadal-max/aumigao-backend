"""Enforcement BLOQUEANTE do aceite legal em 2 camadas (servidor).

Cadastro/perfil/GETs continuam livres; acoes OPERACIONAIS (criar passeio, aceitar
passeio, criar pagamento, assinar plano) exigem aceite. 403 com o shape do contrato:

    {"detail": {"code": "legal_acceptance_required",
                "scope": "platform" | "tenant",
                "tenant_id": <str, so no scope tenant>}}

Plataforma e SEMPRE exigida. Tenant e exigida quando ha tenant ativo na request.
"""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes.legal import LEGAL_VERSION
from app.services import legal_status_service as status_svc


def _enforcement_enabled() -> bool:
    """Enforcement LIGADO por padrao (fail-closed em producao).

    A suite LEGADA de rotas exercita create_walk/create_payment/accept/subscribe
    sem passar pelo fluxo de aceite; ela roda com LEGAL_ACCEPTANCE_ENFORCED=false
    (definido no conftest via setdefault). Os testes NOVOS do aceite ligam a flag
    explicitamente. Mesmo padrao do REQUIRE_PAYMENT_BEFORE_MATCHING.
    """
    return os.getenv("LEGAL_ACCEPTANCE_ENFORCED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _active_tenant_id(request: Request, user: User) -> str | None:
    return getattr(request.state, "tenant_id", None) or getattr(user, "tenant_id", None)


def enforce_legal_acceptance(
    request: Request,
    user: User,
    db: Session,
    scopes: tuple[str, ...] = ("platform", "tenant"),
) -> None:
    """Levanta 403 se faltar aceite em qualquer camada exigida."""
    if not _enforcement_enabled():
        return
    if "platform" in scopes:
        platform = status_svc.platform_status(db, user, LEGAL_VERSION)
        if not platform["accepted"]:
            raise HTTPException(
                status_code=403,
                detail={"code": "legal_acceptance_required", "scope": "platform"},
            )

    if "tenant" in scopes:
        tenant_id = _active_tenant_id(request, user)
        # Tenant so e exigido quando ha tenant ativo na request.
        if tenant_id:
            tenant = status_svc.tenant_status(db, user, tenant_id)
            if tenant is not None and not tenant["accepted"]:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "code": "legal_acceptance_required",
                        "scope": "tenant",
                        "tenant_id": tenant_id,
                    },
                )


def require_legal_acceptance(scopes: tuple[str, ...] = ("platform", "tenant")):
    """Fabrica de dependency FastAPI para exigir aceite nas camadas informadas."""

    def _dep(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> None:
        enforce_legal_acceptance(request, user, db, scopes=scopes)

    return _dep
