"""Serviço de exclusividade de passeador por tenant (Fase 1 — DORMENTE).

Em Fase 1, WalkerNetworkProfile.exclusive_tenant_id é SEMPRE NULL (nenhuma UX
seta o campo). Portanto, todos os guards passam reto e o comportamento atual
é preservado. O enforcement real entra em Fase 2, quando o super_admin puder
setar o campo via UX.

NÃO faz commit: quem chama é responsável pelo commit (padrão do projeto).
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.walker_network_profile import WalkerNetworkProfile


def _get_profile(db: Session, walker_user_id: str) -> Optional[WalkerNetworkProfile]:
    return (
        db.query(WalkerNetworkProfile)
        .filter(WalkerNetworkProfile.walker_user_id == walker_user_id)
        .first()
    )


def get_exclusive_tenant_id(db: Session, walker_user_id: str) -> Optional[str]:
    """Retorna o tenant ao qual o passeador é exclusivo, ou None se não exclusivo."""
    p = _get_profile(db, walker_user_id)
    return p.exclusive_tenant_id if p else None


def walker_exclusivity_ok(db: Session, walker_user_id: str, tenant_id: str) -> bool:
    """True se o walker pode operar no tenant: não-exclusivo, ou exclusivo DESTE tenant.

    Em F1 exclusive_tenant_id é sempre NULL → retorna True para todos os walkers.
    """
    ex = get_exclusive_tenant_id(db, walker_user_id)
    return ex is None or ex == tenant_id


def assert_walker_link_allowed(
    db: Session,
    walker_user_id: str,
    tenant_id: str,
    access_type: str,
) -> None:
    """Guarda ao conceder/alterar vínculo. DORMENTE em F1 (exclusive_tenant_id sempre NULL).

    Casos verificados:
    - Se walker já é exclusivo de OUTRO tenant → 409.
    - Se está criando/mudando para tenant_exclusive → exigir 0 outros vínculos ATIVOS
      com outros tenants (para não ferir o contrato de exclusividade).
    """
    ex = get_exclusive_tenant_id(db, walker_user_id)
    if ex is not None and ex != tenant_id:
        raise HTTPException(
            status_code=409,
            detail="Passeador é exclusivo de outro tenant.",
        )
    if access_type == "tenant_exclusive":
        outros = (
            db.query(TenantWalkerAccess)
            .filter(
                TenantWalkerAccess.walker_user_id == walker_user_id,
                TenantWalkerAccess.status == "active",
                TenantWalkerAccess.tenant_id != tenant_id,
            )
            .count()
        )
        if outros > 0:
            raise HTTPException(
                status_code=409,
                detail="Passeador tem vínculos ativos com outros tenants; não pode ser exclusivo.",
            )


def set_walker_exclusive(db: Session, walker_user_id: str, tenant_id: str) -> None:
    """Marca o passeador como exclusivo do tenant (UX super_admin — Fase 2).

    Pré-condição: sem vínculos ativos com OUTROS tenants (verificar antes via
    assert_walker_link_allowed).
    NÃO faz commit.
    """
    outros = (
        db.query(TenantWalkerAccess)
        .filter(
            TenantWalkerAccess.walker_user_id == walker_user_id,
            TenantWalkerAccess.status == "active",
            TenantWalkerAccess.tenant_id != tenant_id,
        )
        .count()
    )
    if outros > 0:
        raise HTTPException(
            status_code=409,
            detail="Passeador tem vínculos ativos com outros tenants.",
        )
    p = _get_profile(db, walker_user_id)
    if p is None:
        p = WalkerNetworkProfile(walker_user_id=walker_user_id)
        db.add(p)
    p.exclusive_tenant_id = tenant_id


def release_walker_exclusive(db: Session, walker_user_id: str) -> None:
    """Remove a exclusividade do passeador (volta para rede compartilhada).

    NÃO faz commit.
    """
    p = _get_profile(db, walker_user_id)
    if p:
        p.exclusive_tenant_id = None
