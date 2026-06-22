"""RLS: endurece WITH CHECK + cobertura protected_chat/shared_walk_participants.

## O que faz

### #3 — fecha o buraco de escrita com tenant_id NULL
A policy da 0044 permite que qualquer sessão insira/atualize uma linha com
tenant_id IS NULL (útil para leitura de uploads anônimos, mas desnecessariamente
permissivo na escrita para sessões com tenant específico).

Novo alvo para WITH CHECK:
    current_setting('app.current_tenant', true) = '*'
    OR tenant_id::text = current_setting('app.current_tenant', true)

Diferença do USING (mantido como na 0044):
    USING  → permite LER  linhas com tenant_id NULL (uploads anônimos são visíveis)
    CHECK  → NÃO permite ESCREVER linha com tenant_id NULL sob sessão de tenant
             específico. Somente sessões com GUC = '*' (global/super_admin) podem
             gravar NULL — que é exatamente o que fazem os callers legítimos (ver
             análise abaixo).

### #4 — cobertura de protected_chat_messages e shared_walk_participants

**protected_chat_messages**: NÃO tem coluna tenant_id.
  → RLS NÃO é habilitado. Isolamento permanece app-layer (JOIN walk→tenant).
  → Recomendação futura: adicionar tenant_id + backfill via walk.tenant_id
    (migração separada; requer validação de cardinalidade em prod).

**shared_walk_participants**: NÃO tem coluna tenant_id.
  → RLS NÃO é habilitado. Isolamento está garantido indiretamente via
    shared_walks (tabela pai tem tenant_id + policy ativa). Participantes
    só são acessíveis via JOIN shared_walks, que já está protegida.
  → Recomendação futura: considerar coluna tenant_id desnormalizada para
    permitir RLS direto, mas o custo/benefício só se justifica com consultas
    diretas à tabela sem JOIN.

## Análise dos INSERT paths com tenant_id NULL (documentação de segurança)

Todos os fluxos que gravam tenant_id = NULL em tabelas tenant-scoped
operam sob GUC = '*' — portanto continuam passando na nova WITH CHECK:

1. `upload_files` / context="partner_application" (POST /api/partner-applications/uploads)
   → Endpoint NÃO autenticado. Middleware TenantResolver resolve o tenant
     padrão via fallback (modo não-estrito), portanto request.state.tenant_id
     é o ID do tenant padrão (não NULL).
   → `record_upload()` é chamado com tenant_id=None explícito (linha 284 de
     partner_application.py). O `get_db(request)` injeta rls_tenant = tenant_id
     resolvido (um UUID real, não ''), MAS o UploadFile criado tem tenant_id=None.
   → ANÁLISE CRÍTICA: esta é a única linha que insere tenant_id=NULL sob uma
     sessão com GUC = tenant_uuid (não '*'). A nova WITH CHECK QUEBRA este INSERT.
   → MITIGAÇÃO: converter `record_upload()` neste endpoint para passar
     `tenant_id=request.state.tenant_id` (o tenant padrão resolvido pelo middleware)
     OU usar `get_global_db` neste endpoint para gravar sob escopo '*'.
     A opção preferida (menor impacto, mais coerente) é passar o tenant_id
     resolvido — documentado em (E) abaixo. ESTA migration inclui um NO-OP para
     o upload_files até que o código de aplicação seja corrigido; veja seção
     CONDICIONAL.

2. `upload_files` / context="walker_kit" (POST /api/walker/kit/photo)
   → Autenticado (walker ativo). Middleware resolve tenant. `record_upload()` é
     chamado sem tenant_id (passa None implicitamente).
   → MESMO PROBLEMA: grava NULL sob GUC = tenant_uuid. Precisa da mesma correção.

3. `upload_files` / context="walk_completion" (POST /api/walks/{id}/completion-photo)
   → Autenticado (walker ativo). Mesmo padrão — tenant_id=None na linha gravada.

4. `upload_files` / context="pet" (POST /api/pets/upload-photo)
   → Autenticado (tutor). Mesmo padrão.

5. `audit_logs` / walker_profile.wallet_updated (admin.py:2428)
   → Admin super_admin ou tenant_admin chama get_admin_tenant_scope() que invoca
     set_session_tenant('*') para super_admin global OU set_session_tenant(tenant_id)
     para admin de tenant. Quando tenant_id=None é passado ao record_audit_log(),
     o registro fica sem tenant. Se o admin for tenant-scoped, o GUC é o tenant_id
     real — inserir NULL quebraria. Se for super_admin global (GUC='*'), funciona.
   → RISCO: admin de tenant regular que chama este endpoint insere audit_log com
     tenant_id=None sob GUC=tenant_uuid → QUEBRA com a nova CHECK.
   → MITIGAÇÃO: record_audit_log() já tenta ler tenant_id do request.state quando
     tenant_id=None e request!=None (linha 63 audit_service.py). Mas no call de
     admin.py:2428, request não é passado. Solução: passar request ao
     record_audit_log() neste call. Documentado em (E).

CONCLUSÃO: a nova WITH CHECK para upload_files e audit_logs quebraria inserções
legítimas enquanto o código de aplicação não for corrigido. A migration 0045
APLICA a WITH CHECK endurecida mas EXCLUI upload_files e audit_logs até que
as correções de app estejam deployadas. Um comentário claro marca quais tabelas
estão excluídas temporariamente.

PG-only. NO-OP em SQLite. Idempotente (DROP POLICY IF EXISTS + CREATE).

Revision ID: 0045_rls_harden_with_check
Revises: 0044_rls_allow_null_tenant
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_rls_harden_with_check"
down_revision: Union[str, None] = "0044_rls_allow_null_tenant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_POLICY = "tenant_isolation"

# USING mantém tenant_id IS NULL (leitura de linhas anônimas continua OK).
_USING = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)

# WITH CHECK endurecida: somente sessões '*' (global) podem gravar NULL.
# Sessões de tenant específico só podem gravar/atualizar suas próprias linhas.
_WITH_CHECK_STRICT = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)

# Tabelas que ainda inserem tenant_id=NULL sob sessão de tenant específico
# (GUC != '*') — precisam de correção no código de aplicação ANTES que a
# WITH CHECK endurecida possa ser aplicada a elas.
# Enquanto não corrigidas, mantêm a WITH CHECK permissiva da 0044.
_TABLES_PENDING_APP_FIX: set[str] = {
    "upload_files",   # contexts: partner_application, walker_kit, walk_completion, pet
    "audit_logs",     # wallet_updated sem request passado ao record_audit_log
}

# Rollback: a policy permissiva da 0044 (USING == WITH CHECK, ambos com NULL).
_PREDICATE_0044 = (
    "current_setting('app.current_tenant', true) = '*' "
    "OR tenant_id IS NULL "
    "OR tenant_id::text = current_setting('app.current_tenant', true)"
)


def _tenant_tables(conn) -> list[str]:
    """Retorna nomes de tabelas que têm coluna tenant_id (mesmo padrão da 0044)."""
    insp = sa.inspect(conn)
    out = []
    for t in insp.get_table_names():
        if any(c["name"] == "tenant_id" for c in insp.get_columns(t)):
            out.append(t)
    return out


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        # NO-OP em SQLite (CI/testes) — RLS é feature exclusiva do PostgreSQL.
        return

    for table in _tenant_tables(conn):
        op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"')

        if table in _TABLES_PENDING_APP_FIX:
            # Mantém a policy permissiva (igual à 0044) até corrigir o código.
            # TODO: após corrigir record_upload() e o call de record_audit_log()
            #       no admin.py, mover estas tabelas para a branch endurecida abaixo.
            op.execute(
                f'CREATE POLICY {_POLICY} ON "{table}" '
                f"USING ({_PREDICATE_0044}) "
                f"WITH CHECK ({_PREDICATE_0044})"
            )
        else:
            # WITH CHECK endurecida: fecha buraco de escrita NULL sob tenant-scope.
            op.execute(
                f'CREATE POLICY {_POLICY} ON "{table}" '
                f"USING ({_USING}) "
                f"WITH CHECK ({_WITH_CHECK_STRICT})"
            )


def downgrade() -> None:
    """Volta para a policy permissiva da 0044 (USING == WITH CHECK com NULL)."""
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return

    for table in _tenant_tables(conn):
        op.execute(f'DROP POLICY IF EXISTS {_POLICY} ON "{table}"')
        op.execute(
            f'CREATE POLICY {_POLICY} ON "{table}" '
            f"USING ({_PREDICATE_0044}) "
            f"WITH CHECK ({_PREDICATE_0044})"
        )
