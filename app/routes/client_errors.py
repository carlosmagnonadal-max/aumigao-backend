"""Ingestão de erros do app mobile — residência BR via Cloud Logging / Error Reporting.

POST /client-errors: endpoint público (sem auth) que recebe erros do app
React Native e os loga como structured logs para o Cloud Logging. O Google
Cloud Error Reporting agrupa automaticamente erros com stack trace.

Rate limit: 60/min por IP (in-memory, com fallback idêntico ao padrão auth.py).
Tamanho: limitado via Field(max_length=...) no schema Pydantic.
Segurança: SensitiveDataFilter no root logger redige PII automaticamente;
a rota nunca propaga 500 — logging falho retorna 204 mesmo assim.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.login_rate_limiter import InMemoryLoginRateLimiter

_logger = logging.getLogger("app.client_errors")

router = APIRouter(tags=["observability"])

# Rate limit: 60 requests/min por IP.
# InMemoryLoginRateLimiter é sliding-window — window_seconds=60, max_failures=60.
_rate_limiter = InMemoryLoginRateLimiter(
    max_failures=int(os.getenv("CLIENT_ERROR_RATE_LIMIT", "60")),
    window_seconds=60.0,
)


def _get_client_ip(request: Request) -> str:
    """Extrai IP do cliente respeitando X-Forwarded-For (Cloud Run / proxy)."""
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host or "unknown")


class ClientErrorReport(BaseModel):
    level: Literal["error", "warn"]
    message: str = Field(..., max_length=2000)
    error_type: str | None = Field(default=None, max_length=200)
    stack: str | None = Field(default=None, max_length=8000)
    platform: str | None = Field(default=None, max_length=20)
    app_version: str | None = Field(default=None, max_length=50)
    context: dict[str, Any] | None = None


@router.post("/client-errors", status_code=204)
def ingest_client_error(payload: ClientErrorReport, request: Request) -> Response:
    """Recebe um erro do app mobile e o loga via Cloud Logging.

    - 204: sucesso (ou logging falhou internamente — swallowed).
    - 422: payload inválido (Pydantic validation).
    - 429: rate limit por IP excedido.
    """
    client_ip = _get_client_ip(request)

    if _rate_limiter.is_blocked(client_ip):
        return Response(
            content='{"detail":"Too many error reports. Try again later."}',
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": "60"},
        )
    _rate_limiter.record_failure(client_ip)

    try:
        # Extrai campos de contexto com limite de segurança: nunca loga o dict
        # completo cru — extrai só os campos conhecidos e pequenos para o extra.
        ctx = payload.context or {}
        screen = str(ctx.get("screen", "") or "")[:200]
        action = str(ctx.get("action", "") or "")[:200]
        # Evita conflito com campos reservados do logging filter (user_id / tenant_id).
        client_user_id = str(ctx.get("userId", "") or ctx.get("user_id", "") or "")[:100]
        walker_id = str(ctx.get("walkerId", "") or ctx.get("walker_id", "") or "")[:100]

        # Monta a mensagem principal: Cloud Error Reporting detecta stack trace
        # quando o texto contém linhas com espaços + "at " ou "File ", etc.
        # Formato: "<error_type>: <message>\n<stack>" — compatível com GCE grouping.
        error_type_label = payload.error_type or "ClientError"
        log_message = f"{error_type_label}: {payload.message}"
        if payload.stack:
            log_message = f"{log_message}\n{payload.stack}"

        extra: dict[str, Any] = {
            "client_platform": payload.platform,
            "app_version": payload.app_version,
            "screen": screen or None,
            "action": action or None,
            "client_user_id": client_user_id or None,
            "client_walker_id": walker_id or None,
        }
        # Remove None values para não poluir o structured log.
        extra = {k: v for k, v in extra.items() if v is not None}

        if payload.level == "error":
            _logger.error(log_message, extra=extra)
        else:
            _logger.warning(log_message, extra=extra)

    except Exception:
        # Logging nunca pode gerar 500 para o cliente — swallow silencioso.
        pass

    return Response(status_code=204)
