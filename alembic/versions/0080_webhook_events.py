"""webhook_events: dedup persistente de webhooks do provedor (Asaas) por event_id.

Fecha o gap documentado em payments.py (idempotência sem dedup persistente):
um reenvio do mesmo evento poderia reaplicar efeito financeiro. UNIQUE em
event_id + INSERT-if-not-exists no início do handler.

Escopo GLOBAL: o webhook processa qualquer tenant sob rls_tenant="*", e a tabela
não tem tenant_id. A policy RLS permite apenas o escopo global ('*') — que é
exatamente o contexto do webhook (get_global_db). Nenhuma sessão tenant-scoped
precisa ler/escrever aqui.

Revision ID: 0080_webhook_events
Revises: 0079_commission_percent_default
"""
from alembic import op
import sqlalchemy as sa

revision = "0080_webhook_events"
down_revision = "0079_commission_percent_default"
branch_labels = None
depends_on = None

# Só o escopo global ('*') acessa (contexto do webhook). Sem tenant_id na tabela.
_POLICY = "current_setting('app.current_tenant', true) = '*'"


def upgrade() -> None:
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False, server_default="asaas"),
        sa.Column("event_type", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "uq_webhook_events_event_id", "webhook_events", ["event_id"], unique=True
    )

    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text('ALTER TABLE "webhook_events" ENABLE ROW LEVEL SECURITY'))
        conn.execute(sa.text('DROP POLICY IF EXISTS tenant_isolation ON "webhook_events"'))
        conn.execute(sa.text(
            'CREATE POLICY tenant_isolation ON "webhook_events" '
            f"USING ({_POLICY}) WITH CHECK ({_POLICY})"
        ))


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text('DROP POLICY IF EXISTS tenant_isolation ON "webhook_events"'))
        conn.execute(sa.text('ALTER TABLE "webhook_events" DISABLE ROW LEVEL SECURITY'))
    op.drop_index("uq_webhook_events_event_id", table_name="webhook_events")
    op.drop_table("webhook_events")
