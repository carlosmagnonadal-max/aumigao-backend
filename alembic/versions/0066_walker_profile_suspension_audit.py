"""walker_profiles: trilha de suspensao/bloqueio (suspension_reason, status_changed_by, status_changed_at)

Rationale: todo bloqueio/suspensao/restricao de um passeador precisa de motivo
registrado (devido processo — risco trabalhista STJ Tema 1291 / art. 3 CLT).
Estas colunas complementam o AuditLog centralizado permitindo consulta direta
ao registro sem join.

Revision ID: 0066_walker_profile_suspension_audit
Revises: 0065_walker_earning_void
"""
import sqlalchemy as sa
from alembic import op

revision = "0066_walker_profile_suspension_audit"
down_revision = "0065_walker_earning_void"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Motivo do ultimo bloqueio/suspensao (obrigatorio via API para status restritivos).
    op.add_column(
        "walker_profiles",
        sa.Column("suspension_reason", sa.Text(), nullable=True),
    )
    # ID do admin que realizou a ultima alteracao de status restritivo.
    op.add_column(
        "walker_profiles",
        sa.Column("status_changed_by", sa.String(), nullable=True),
    )
    # Timestamp da ultima alteracao de status restritivo (UTC).
    op.add_column(
        "walker_profiles",
        sa.Column("status_changed_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("walker_profiles", "status_changed_at")
    op.drop_column("walker_profiles", "status_changed_by")
    op.drop_column("walker_profiles", "suspension_reason")
