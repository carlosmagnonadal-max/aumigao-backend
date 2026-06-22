"""
Logging estruturado simples.

Configura o root logger uma única vez com:
  - Formato: %(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s
    (ou JSON quando LOG_FORMAT=json, que é o default fora de ambiente local)
  - Filter que injeta request_id, user_id, tenant_id dos ContextVars
  - SensitiveDataFilter que redige PII de todos os records (LGPD)
  - Nível configurável via env LOG_LEVEL (default INFO)

Seguro para chamar múltiplas vezes (idempotente via flag de módulo).
Não duplica handlers do uvicorn.
"""
from __future__ import annotations

import logging
import os

# Importado aqui para que o módulo já exponha os ContextVars mesmo
# antes de configure_logging() ser chamada.
from app.core.request_context import request_id_var, tenant_id_var, user_id_var
from app.core.log_masking import SensitiveDataFilter

_configured = False


class _RequestContextFilter(logging.Filter):
    """Injeta request_id, user_id e tenant_id nos LogRecords a partir dos ContextVars."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")  # type: ignore[attr-defined]
        record.user_id = user_id_var.get("-")        # type: ignore[attr-defined]
        record.tenant_id = tenant_id_var.get("-")    # type: ignore[attr-defined]
        return True


# Keep the old name as alias so any code that imported _RequestIdFilter still works.
_RequestIdFilter = _RequestContextFilter


def _make_formatter() -> logging.Formatter:
    """Retorna formatter JSON ou texto plano de acordo com LOG_FORMAT."""
    log_format = os.getenv("LOG_FORMAT", "").lower()
    # Default: JSON em prod/cloud (qualquer valor não-"text"); texto quando explicitamente "text".
    use_json = log_format != "text"
    if use_json:
        try:
            from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore[import]
            return JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(request_id)s %(user_id)s %(tenant_id)s %(message)s",
                rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
            )
        except ImportError:
            # python-json-logger não instalado — cai para texto plano sem ruído.
            pass
    fmt = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    return logging.Formatter(fmt)


def configure_logging() -> None:
    """Configura o root logger de forma idempotente."""
    global _configured
    if _configured:
        return
    _configured = True

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    formatter = _make_formatter()
    ctx_filter = _RequestContextFilter()
    pii_filter = SensitiveDataFilter()

    root_logger = logging.getLogger()

    # Evita duplicar handlers se o uvicorn (ou outro framework) já configurou.
    if root_logger.handlers:
        for handler in root_logger.handlers:
            handler.setFormatter(formatter)
            if not any(isinstance(f, _RequestContextFilter) for f in handler.filters):
                handler.addFilter(ctx_filter)
            if not any(isinstance(f, SensitiveDataFilter) for f in handler.filters):
                handler.addFilter(pii_filter)
        root_logger.setLevel(level)
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        handler.addFilter(ctx_filter)
        handler.addFilter(pii_filter)
        root_logger.addHandler(handler)
        root_logger.setLevel(level)
