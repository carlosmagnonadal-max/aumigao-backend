"""Sprint 15 (passo 1) — tabelas de RBAC/ABAC

Cria roles, permissions, role_permissions e user_role_assignments. 100% aditivo
(tabelas novas, vazias) e reversível. Não toca nenhuma tabela existente; o `role`
string em users continua valendo até a migração de auth ser concluída.

Revision ID: 0004_rbac_tables
Revises: 0003_backfill_tenant_id
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_rbac_tables"
down_revision: Union[str, None] = "0003_backfill_tenant_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("scope_type", sa.String(), nullable=False, server_default="global"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_roles_name", "roles", ["name"], unique=True)

    op.create_table(
        "permissions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("module", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_permissions_key", "permissions", ["key"], unique=True)
    op.create_index("ix_permissions_module", "permissions", ["module"], unique=False)

    op.create_table(
        "role_permissions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("role_id", sa.String(), nullable=False),
        sa.Column("permission_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
        sa.ForeignKeyConstraint(["permission_id"], ["permissions.id"]),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permissions"),
    )
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"], unique=False)
    op.create_index(
        "ix_role_permissions_permission_id", "role_permissions", ["permission_id"], unique=False
    )

    op.create_table(
        "user_role_assignments",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("role_id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("tenant_unit_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"]),
    )
    op.create_index("ix_user_role_assignments_user_id", "user_role_assignments", ["user_id"], unique=False)
    op.create_index("ix_user_role_assignments_role_id", "user_role_assignments", ["role_id"], unique=False)
    op.create_index("ix_user_role_assignments_tenant_id", "user_role_assignments", ["tenant_id"], unique=False)
    op.create_index(
        "ix_user_role_assignments_tenant_unit_id", "user_role_assignments", ["tenant_unit_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("user_role_assignments")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
