from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.tenant_tutor_access import TenantTutorAccess


def is_tutor_eligible_for_tenant(db: Session, tenant_id: str, tutor_user_id: str) -> bool:
    """True se o tutor tem vínculo ACTIVE com o tenant (gate de criação de reserva)."""
    return (
        db.query(TenantTutorAccess.id)
        .filter(
            TenantTutorAccess.tenant_id == tenant_id,
            TenantTutorAccess.tutor_user_id == tutor_user_id,
            TenantTutorAccess.status == "active",
        )
        .first()
        is not None
    )
