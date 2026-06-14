from __future__ import annotations

import logging
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.services.tenant_resolver_service import resolve_tenant_identity

logger = logging.getLogger(__name__)


class TenantResolverMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, session_factory: Callable):
        super().__init__(app)
        self.session_factory = session_factory

    async def dispatch(self, request: Request, call_next):
        try:
            # resolve_tenant_identity usa cache (chaveado pelo session_factory): só
            # toca o banco no cache-miss, evitando 2-4 queries por requisição.
            identity = resolve_tenant_identity(request, self.session_factory)
            request.state.tenant = None
            request.state.tenant_id = identity[0] if identity else None
            request.state.tenant_slug = identity[1] if identity else None
        except Exception as exc:
            logger.warning("tenant_resolver_middleware_fallback_failed error=%s", exc)
            request.state.tenant = None
            request.state.tenant_id = None
            request.state.tenant_slug = None

        return await call_next(request)
