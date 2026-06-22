"""
ContextVars compartilhados para contexto de request.

Importado por:
- app/core/logging_config.py  (filter de logging — injeta nos LogRecords)
- app/middleware/request_context.py (middleware que seta os valores por request)
- app/main.py (exception handler que lê request_id)
- Código de rotas/dependências que precisam setar user_id/tenant_id após autenticação.

Convenção de valor padrão: "-" indica "fora de contexto de request".
"""
from __future__ import annotations

from contextvars import ContextVar

# Valor padrão "-" indica "fora de contexto de request"
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Preenchido pelo RequestContextMiddleware a partir de request.state.tenant_id
# (resolvido pelo TenantMiddleware), se disponível; "-" caso contrário.
tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="-")

# Preenchido após autenticação (get_current_user ou equivalente).
# O middleware não consegue resolver user_id antes da rota; rotas/dependências
# devem setar este ContextVar logo após validar o token.
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")
