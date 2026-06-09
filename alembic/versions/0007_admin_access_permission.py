"""Sprint 15 (passo 4) — permissão baseline admin.access

Adiciona a permissão `admin.access` (equivalente ao "é admin" antigo) e concede a
global_admin e tenant_admin. Usada a nível de router em admin.py/complaints para
aposentar o require_admin (que dependia do role string) — o RBAC passa a ser a
única fonte de autorização. Rotas já migradas mantêm sua permissão granular.

NÃO concedida a tenant_operator/unit_operator: preserva o comportamento atual
(eles não eram "admin" e não acessavam o painel admin core).

Idempotente. Downgrade remove o que inseriu.

Revision ID: 0007_admin_access_permission
Revises: 0006_expand_rbac_catalog
Create Date: 2026-06-09
"""
from datetime import datetime
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision: str = "0007_admin_access_permission"
down_revision: Union[str, None] = "0006_expand_rbac_catalog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_GRANTS = ["global_admin", "tenant_admin"]


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.utcnow()

    bind.execute(
        sa.text(
            "INSERT INTO permissions (id, key, module, action, created_at, updated_at) "
            "VALUES (:id, 'admin.access', 'admin', 'access', :t, :t) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"id": str(uuid4()), "t": now},
    )

    role_ids = {r.name: r.id for r in bind.execute(sa.text("SELECT id, name FROM roles"))}
    perm_id = bind.execute(
        sa.text("SELECT id FROM permissions WHERE key = 'admin.access'")
    ).scalar()

    for role_name in ROLE_GRANTS:
        role_id = role_ids.get(role_name)
        if not role_id or not perm_id:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                "VALUES (:id, :r, :p, :t) ON CONFLICT (role_id, permission_id) DO NOTHING"
            ),
            {"id": str(uuid4()), "r": role_id, "p": perm_id, "t": now},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE key = 'admin.access')"
        )
    )
    bind.execute(sa.text("DELETE FROM permissions WHERE key = 'admin.access'"))
