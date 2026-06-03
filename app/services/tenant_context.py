from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.tenant import Tenant
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG, ensure_default_tenant


def get_default_tenant(db: Session) -> Tenant:
    """Return the beta default tenant without committing when it already exists."""
    tenant = db.query(Tenant).filter(Tenant.slug == DEFAULT_TENANT_SLUG).first()
    if tenant:
        return tenant
    return ensure_default_tenant(db)


def resolve_current_tenant(db: Session, request: Request | None = None) -> Tenant:
    """Resolve the current tenant for this request.

    Today, every operational record belongs to the default Aumigao tenant.
    Future white-label resolution can come from:
    - subdomain
    - custom domain
    - dedicated app
    - request header
    - token claims
    - authenticated user's tenant

    This sprint only exposes the context layer; it does not apply tenant
    filtering or change operational behavior.
    """
    tenant_id = getattr(getattr(request, "state", None), "tenant_id", None) if request else None
    if tenant_id:
        tenant = db.get(Tenant, tenant_id)
        if tenant:
            return tenant
    return get_default_tenant(db)


def resolve_current_tenant_id(db: Session, request: Request | None = None) -> str:
    return resolve_current_tenant(db, request).id


def get_current_tenant(request: Request, db: Session = Depends(get_db)) -> Tenant:
    return resolve_current_tenant(db, request)
