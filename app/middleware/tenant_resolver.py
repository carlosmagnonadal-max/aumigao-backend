from __future__ import annotations

import logging
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.services.tenant_resolver_service import resolve_tenant_identity
from app.services.tenant_status_service import get_tenant_status, is_path_allowlisted

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

        # Enforcement de suspensão: bloqueia rotas não-allowlistadas para tenants suspensos.
        # Fica FORA do try/except acima para garantir que:
        #   (a) falhas na resolução de identity (tenant_id=None) não bloqueiam nada (fail-open);
        #   (b) o JSONResponse 403 sai direto do dispatch sem cair no except genérico.
        _tid = request.state.tenant_id
        if _tid and not is_path_allowlisted(request.url.path):
            try:
                if get_tenant_status(_tid, self.session_factory) == "suspended":
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": (
                                "Conta suspensa por pendência de pagamento. "
                                "Regularize para reativar."
                            )
                        },
                    )
            except Exception as exc:
                logger.error("tenant_suspension_check_failed error=%s", exc)  # fail-open

        return await call_next(request)
