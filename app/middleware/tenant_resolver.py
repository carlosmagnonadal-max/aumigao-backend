from __future__ import annotations

import logging
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.models.tenant import Tenant
from app.services.tenant_resolver_service import resolve_tenant_from_request

logger = logging.getLogger(__name__)


class TenantResolverMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, session_factory: Callable):
        super().__init__(app)
        self.session_factory = session_factory

    async def dispatch(self, request: Request, call_next):
        try:
            with self.session_factory() as db:
                tenant: Tenant = resolve_tenant_from_request(request, db)
                request.state.tenant = tenant
                request.state.tenant_id = tenant.id
                request.state.tenant_slug = tenant.slug
        except Exception as exc:
            logger.warning("tenant_resolver_middleware_fallback_failed error=%s", exc)
            request.state.tenant = None
            request.state.tenant_id = None
            request.state.tenant_slug = None

        return await call_next(request)
