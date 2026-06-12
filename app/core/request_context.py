"""
ContextVar compartilhado para request_id.

Importado por:
- app/core/logging_config.py  (filter de logging)
- app/middleware/request_context.py (middleware que seta o valor)
- app/main.py (exception handler que lê o valor)
"""
from __future__ import annotations

from contextvars import ContextVar

# Valor padrão "-" indica "fora de contexto de request"
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
