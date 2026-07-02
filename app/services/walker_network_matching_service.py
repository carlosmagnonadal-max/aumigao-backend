import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_network_profile import WalkerNetworkProfile
from app.models.walker_profile import WalkerProfile

MATCHING_ACCESS_TYPES = ("shared_network", "tenant_exclusive")


def get_tenant_eligible_walker_ids(db: Session, tenant_id: str) -> list[str]:
    """Return walker user ids allowed by the tenant network foundation.

    This service only describes the tenant-aware pool. Operational matching still
    applies its existing availability, score, region, and safety rules.

    Fase 1 — exclusivity guard (DORMENTE):
      LEFT OUTER JOIN com WalkerNetworkProfile + filtro null-safe garante que
      passeadores exclusivos de OUTRO tenant não entrem no pool. Como
      exclusive_tenant_id é sempre NULL na F1, o filtro is_(None) mantém todos
      os walkers atuais → pool inalterado. REGRESSÃO ZERO.
    """
    rows = (
        db.query(TenantWalkerAccess.walker_user_id)
        .join(User, User.id == TenantWalkerAccess.walker_user_id)
        .join(WalkerProfile, WalkerProfile.user_id == TenantWalkerAccess.walker_user_id)
        # LEFT JOIN: walkers SEM WalkerNetworkProfile continuam elegíveis (exclusive=NULL implícito)
        .outerjoin(
            WalkerNetworkProfile,
            WalkerNetworkProfile.walker_user_id == TenantWalkerAccess.walker_user_id,
        )
        .filter(
            TenantWalkerAccess.tenant_id == tenant_id,
            TenantWalkerAccess.status == "active",
            TenantWalkerAccess.access_type.in_(MATCHING_ACCESS_TYPES),
            User.role == "walker",
            User.is_active.is_(True),
            WalkerProfile.status == "active",
            WalkerProfile.active_as_walker.is_(True),
            # F3.2: gate de requisitos extras por tenant. Linhas existentes são true (default) →
            # grandfather automático; só exclui quando o admin marcou requirements_met=false.
            TenantWalkerAccess.requirements_met.is_(True),
            # Null-safe: passa se não exclusivo (NULL) ou exclusivo DESTE tenant
            sa.or_(
                WalkerNetworkProfile.exclusive_tenant_id.is_(None),
                WalkerNetworkProfile.exclusive_tenant_id == tenant_id,
            ),
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def is_walker_eligible_for_tenant(db: Session, tenant_id: str, walker_user_id: str) -> bool:
    """Check whether one walker is enabled for a tenant matching pool.

    Fase 1 — exclusivity guard (DORMENTE): mesmo outerjoin null-safe de
    get_tenant_eligible_walker_ids. exclusive_tenant_id é sempre NULL em F1.
    """
    return (
        db.query(TenantWalkerAccess.id)
        .join(User, User.id == TenantWalkerAccess.walker_user_id)
        .join(WalkerProfile, WalkerProfile.user_id == TenantWalkerAccess.walker_user_id)
        # LEFT JOIN: walker sem profile → exclusive=NULL → elegível
        .outerjoin(
            WalkerNetworkProfile,
            WalkerNetworkProfile.walker_user_id == TenantWalkerAccess.walker_user_id,
        )
        .filter(
            TenantWalkerAccess.tenant_id == tenant_id,
            TenantWalkerAccess.walker_user_id == walker_user_id,
            TenantWalkerAccess.status == "active",
            TenantWalkerAccess.access_type.in_(MATCHING_ACCESS_TYPES),
            User.role == "walker",
            User.is_active.is_(True),
            WalkerProfile.status == "active",
            WalkerProfile.active_as_walker.is_(True),
            # F3.2: gate de requisitos extras por tenant. Linhas existentes são true (default) →
            # grandfather automático; só exclui quando o admin marcou requirements_met=false.
            TenantWalkerAccess.requirements_met.is_(True),
            # Null-safe: passa se não exclusivo (NULL) ou exclusivo DESTE tenant
            sa.or_(
                WalkerNetworkProfile.exclusive_tenant_id.is_(None),
                WalkerNetworkProfile.exclusive_tenant_id == tenant_id,
            ),
        )
        .first()
        is not None
    )


def initial_requirements_met(db: Session, tenant_id: str) -> bool:
    """F3.2: False se o tenant tem requisitos extras (vínculo NOVO nasce pendente);
    True caso contrário (comportamento legado). Não rebaixa vínculos já existentes."""
    from app.models.tenant import Tenant

    tenant = db.get(Tenant, tenant_id)
    reqs = getattr(tenant, "walker_extra_requirements", None) if tenant else None
    return not (isinstance(reqs, list) and len(reqs) > 0)


def tenant_network_blocked_by_plan(db: Session, tenant_id: str) -> bool:
    """True se o PLANO do tenant proíbe acesso à Rede Aumigão de passeadores.

    Plano `free` ("Começar") = REDE DESLIGADA (decisão do Carlos 2026-07-02).
    O reverse trial (21d) libera: durante o trial o plano EFETIVO é "pro", então
    NÃO bloqueia. Tenants pro/enterprise nunca bloqueiam aqui → zero-regressão.
    """
    from app.models.tenant import Tenant
    from app.services.tenant_free_plan_service import effective_tenant_plan, is_free_plan

    tenant = db.get(Tenant, tenant_id) if tenant_id else None
    if tenant is None:
        return False
    # Plano EFETIVO: free em trial ativo vira "pro" → rede liberada no trial.
    return is_free_plan(effective_tenant_plan(tenant))


def get_matching_pool_for_tenant(db: Session, tenant_id: str) -> list[str]:
    """Return the tenant pool used by matching as a defensive foundation.

    Future iterations can add region, availability, reputation, service radius,
    specialties, tenant plan, and operational priority on top of this pool.

    Plano free: a REDE é desligada → pool VAZIO (tenant free só usa passeadores
    próprios, que não passam por este pool de rede). O trial de 21d libera a rede.
    """
    if tenant_network_blocked_by_plan(db, tenant_id):
        return []
    return get_tenant_eligible_walker_ids(db, tenant_id)
