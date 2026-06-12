"""
Logging estruturado simples.

Configura o root logger uma única vez com:
  - Formato: %(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s
  - Filter que injeta o request_id do ContextVar (ou "-" fora de request)
  - Nível configurável via env LOG_LEVEL (default INFO)

Seguro para chamar múltiplas vezes (idempotente via flag de módulo).
Não duplica handlers do uvicorn.
"""
from __future__ import annotations

import logging
import os

# Importado aqui para que o módulo já exponha o ContextVar mesmo
# antes de configure_logging() ser chamada.
from app.core.request_context import request_id_var

_configured = False


class _RequestIdFilter(logging.Filter):
    """Injeta request_id no LogRecord a partir do ContextVar."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")  # type: ignore[attr-defined]
        return True


def configure_logging() -> None:
    """Configura o root logger de forma idempotente."""
    global _configured
    if _configured:
        return
    _configured = True

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    formatter = logging.Formatter(fmt)

    request_id_filter = _RequestIdFilter()

    root_logger = logging.getLogger()

    # Evita duplicar handlers se o uvicorn (ou outro framework) já configurou.
    if root_logger.handlers:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)
            if not any(isinstance(f, _RequestIdFilter) for f in handler.filters):
                handler.addFilter(request_id_filter)
        root_logger.setLevel(level)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(request_id_filter)
        root_logger.addHandler(handler)
        root_logger.setLevel(level)
