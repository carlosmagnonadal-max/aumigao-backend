"""
Middleware de Request-ID e detecção de requests lentos.

- Gera ou reutiliza o header X-Request-ID (uuid4 hex, 12 chars).
- Armazena no ContextVar `request_id_var` (acessível pelo logging e pelo exception handler).
- Adiciona X-Request-ID no response.
- Mede duração; se > 1.0 s, emite warning slow_request.
"""
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.request_context import request_id_var

logger = logging.getLogger(__name__)

SLOW_REQUEST_THRESHOLD_S = 1.0


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # Usa o header enviado pelo cliente (balancer/gateway) ou gera um novo.
        incoming = request.headers.get("X-Request-ID", "").strip()
        request_id = incoming[:12] if incoming else uuid.uuid4().hex[:12]

        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            response: Response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            elapsed = time.perf_counter() - start
            duration_ms = elapsed * 1000
            if elapsed > SLOW_REQUEST_THRESHOLD_S:
                logger.warning(
                    "slow_request method=%s path=%s duration_ms=%.1f request_id=%s",
                    request.method,
                    request.url.path,
                    duration_ms,
                    request_id,
                )
            return response
        finally:
            request_id_var.reset(token)
