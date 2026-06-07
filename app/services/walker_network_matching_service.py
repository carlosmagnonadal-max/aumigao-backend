from sqlalchemy.orm import Session

from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_profile import WalkerProfile

MATCHING_ACCESS_TYPES = ("shared_network", "tenant_exclusive")


def get_tenant_eligible_walker_ids(db: Session, tenant_id: str) -> list[str]:
    """Return walker user ids allowed by the tenant network foundation.

    This service only describes the tenant-aware pool. Operational matching still
    applies its existing availability, score, region, and safety rules.
    """
    rows = (
        db.query(TenantWalkerAccess.walker_user_id)
        .join(User, User.id == TenantWalkerAccess.walker_user_id)
        .join(WalkerProfile, WalkerProfile.user_id == TenantWalkerAccess.walker_user_id)
        .filter(
            TenantWalkerAccess.tenant_id == tenant_id,
            TenantWalkerAccess.status == "active",
            TenantWalkerAccess.access_type.in_(MATCHING_ACCESS_TYPES),
            User.role == "walker",
            User.is_active.is_(True),
            WalkerProfile.status == "active",
            WalkerProfile.active_as_walker.is_(True),
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]


def is_walker_eligible_for_tenant(db: Session, tenant_id: str, walker_user_id: str) -> bool:
    """Check whether one walker is enabled for a tenant matching pool."""
    return (
        db.query(TenantWalkerAccess.id)
        .join(User, User.id == TenantWalkerAccess.walker_user_id)
        .join(WalkerProfile, WalkerProfile.user_id == TenantWalkerAccess.walker_user_id)
        .filter(
            TenantWalkerAccess.tenant_id == tenant_id,
            TenantWalkerAccess.walker_user_id == walker_user_id,
            TenantWalkerAccess.status == "active",
            TenantWalkerAccess.access_type.in_(MATCHING_ACCESS_TYPES),
            User.role == "walker",
            User.is_active.is_(True),
            WalkerProfile.status == "active",
            WalkerProfile.active_as_walker.is_(True),
        )
        .first()
        is not None
    )


def get_matching_pool_for_tenant(db: Session, tenant_id: str) -> list[str]:
    """Return the tenant pool used by matching as a defensive foundation.

    Future iterations can add region, availability, reputation, service radius,
    specialties, tenant plan, and operational priority on top of this pool.
    """
    return get_tenant_eligible_walker_ids(db, tenant_id)
