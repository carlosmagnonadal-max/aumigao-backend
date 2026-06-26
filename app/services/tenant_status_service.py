"""Status do tenant para enforcement de suspensão (Projeto B)."""
from app.models.tenant import Tenant

_ALLOWLIST_PREFIXES = (
    "/admin",
    "/payments/webhooks",
    "/health",
    "/docs",
    "/openapi",
    "/api/v1/admin",
)


def is_path_allowlisted(path: str) -> bool:
    return any(path.startswith(p) for p in _ALLOWLIST_PREFIXES)


def get_tenant_status(tenant_id: str, session_factory) -> str | None:
    if not tenant_id:
        return None
    with session_factory() as db:
        t = db.get(Tenant, tenant_id)
        return t.status if t else None
