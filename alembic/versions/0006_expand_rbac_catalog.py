"""Sprint 15 — expande o catálogo de RBAC para os routers restantes

Adiciona permissões para os módulos que ainda não tinham (reviews, missions,
quality, referrals, walker_network, users) + matching.manage, e atualiza a matriz:
- global_admin passa a ter TODAS as permissões (re-popula, idempotente);
- tenant_admin / tenant_operator recebem as novas permissões relevantes.

Idempotente (ON CONFLICT DO NOTHING). Downgrade remove o que este seed inseriu.

Revision ID: 0006_expand_rbac_catalog
Revises: 0005_seed_rbac
Create Date: 2026-06-09
"""
from datetime import datetime
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision: str = "0006_expand_rbac_catalog"
down_revision: Union[str, None] = "0005_seed_rbac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (key, module, action)
NEW_PERMISSIONS = [
    ("reviews.read", "reviews", "read"),
    ("reviews.manage", "reviews", "manage"),
    ("missions.read", "missions", "read"),
    ("missions.manage", "missions", "manage"),
    ("quality.read", "quality", "read"),
    ("quality.manage", "quality", "manage"),
    ("referrals.read", "referrals", "read"),
    ("referrals.manage", "referrals", "manage"),
    ("walker_network.read", "walker_network", "read"),
    ("walker_network.manage", "walker_network", "manage"),
    ("matching.manage", "matching", "manage"),
    ("users.read", "users", "read"),
    ("users.manage", "users", "manage"),
]

# Permissões novas concedidas a papéis de tenant (global_admin recebe TODAS à parte).
NEW_ROLE_PERMS: dict[str, list[str]] = {
    "tenant_admin": [
        "reviews.read", "reviews.manage",
        "missions.read", "missions.manage",
        "quality.read", "quality.manage",
        "referrals.read", "referrals.manage",
        "matching.manage",
        "users.read",
    ],
    "tenant_operator": [
        "reviews.read", "missions.read", "quality.read", "referrals.read", "matching.manage",
    ],
}


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.utcnow()

    for key, module, action in NEW_PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, key, module, action, created_at, updated_at) "
                "VALUES (:id, :key, :m, :a, :t, :t) ON CONFLICT (key) DO NOTHING"
            ),
            {"id": str(uuid4()), "key": key, "m": module, "a": action, "t": now},
        )

    role_ids = {r.name: r.id for r in bind.execute(sa.text("SELECT id, name FROM roles"))}
    perm_ids = {p.key: p.id for p in bind.execute(sa.text("SELECT id, key FROM permissions"))}

    # global_admin recebe TODAS as permissões (inclui as novas).
    global_admin_id = role_ids.get("global_admin")
    if global_admin_id:
        for pid in perm_ids.values():
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                    "VALUES (:id, :r, :p, :t) ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"id": str(uuid4()), "r": global_admin_id, "p": pid, "t": now},
            )

    # Demais papéis recebem as novas permissões relevantes.
    for role_name, keys in NEW_ROLE_PERMS.items():
        role_id = role_ids.get(role_name)
        if not role_id:
            continue
        for key in keys:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                    "VALUES (:id, :r, :p, :t) ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"id": str(uuid4()), "r": role_id, "p": perm_ids[key], "t": now},
            )


def downgrade() -> None:
    bind = op.get_bind()
    keys = [k for k, _, _ in NEW_PERMISSIONS]
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE key = ANY(:keys))"
        ),
        {"keys": keys},
    )
    bind.execute(sa.text("DELETE FROM permissions WHERE key = ANY(:keys)"), {"keys": keys})
