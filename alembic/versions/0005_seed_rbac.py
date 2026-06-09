"""Sprint 15 (passo 2) — seed de RBAC: permissões, papéis e atribuição inicial

Popula:
  - permissions (catálogo de permissões da spec §7.3)
  - roles (global_admin, tenant_admin, tenant_operator, unit_operator, tutor, walker)
  - role_permissions (matriz papel × permissão)
  - user_role_assignments: mapeia o `role` string atual de cada usuário para um
    papel, garantindo que NINGUÉM perde acesso.

Idempotente (ON CONFLICT DO NOTHING / checagem de existência). Downgrade limpa o
que este seed inseriu.

Revision ID: 0005_seed_rbac
Revises: 0004_rbac_tables
Create Date: 2026-06-09
"""
from datetime import datetime
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op


revision: str = "0005_seed_rbac"
down_revision: Union[str, None] = "0004_rbac_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (key, module, action)
PERMISSIONS = [
    ("walks.read", "walks", "read"),
    ("walks.create", "walks", "create"),
    ("walks.update_status", "walks", "update_status"),
    ("walks.recover", "walks", "recover"),
    ("walks.cancel", "walks", "cancel"),
    ("matching.read", "matching", "read"),
    ("matching.retry", "matching", "retry"),
    ("tutors.read", "tutors", "read"),
    ("tutors.manage", "tutors", "manage"),
    ("walkers.read", "walkers", "read"),
    ("walkers.validate", "walkers", "validate"),
    ("walker_documents.read", "walker_documents", "read"),
    ("walker_documents.approve", "walker_documents", "approve"),
    ("walker_documents.reject", "walker_documents", "reject"),
    ("occurrences.read", "occurrences", "read"),
    ("occurrences.manage", "occurrences", "manage"),
    ("finance.read", "finance", "read"),
    ("finance.manage", "finance", "manage"),
    ("tips.read", "tips", "read"),
    ("tips.manage", "tips", "manage"),
    ("tenants.read", "tenants", "read"),
    ("tenants.manage", "tenants", "manage"),
    ("tenant_units.manage", "tenant_units", "manage"),
    ("branding.read", "branding", "read"),
    ("branding.update", "branding", "update"),
    ("features.manage", "features", "manage"),
    ("settings.manage", "settings", "manage"),
    ("audit_logs.read", "audit_logs", "read"),
    ("alerts.read", "alerts", "read"),
    ("alerts.resolve", "alerts", "resolve"),
]

# (name, scope_type, description)
ROLES = [
    ("global_admin", "global", "Admin global da plataforma Aumigao"),
    ("tenant_admin", "tenant", "Admin de um tenant"),
    ("tenant_operator", "tenant", "Operador de um tenant"),
    ("unit_operator", "unit", "Operador de uma unidade do tenant"),
    ("tutor", "global", "Tutor (cliente)"),
    ("walker", "global", "Passeador"),
]

# Matriz papel -> permissões ("*" = todas)
ROLE_PERMS: dict[str, object] = {
    "global_admin": "*",
    "tenant_admin": [
        "walks.read", "walks.create", "walks.update_status", "walks.recover", "walks.cancel",
        "matching.read", "matching.retry",
        "tutors.read", "tutors.manage",
        "walkers.read", "walkers.validate",
        "walker_documents.read", "walker_documents.approve", "walker_documents.reject",
        "occurrences.read", "occurrences.manage",
        "finance.read", "finance.manage",
        "tips.read", "tips.manage",
        "tenants.read",
        "tenant_units.manage",
        "branding.read", "branding.update",
        "settings.manage",
        "audit_logs.read",
        "alerts.read", "alerts.resolve",
    ],
    "tenant_operator": [
        "walks.read", "walks.update_status", "walks.recover", "walks.cancel",
        "matching.read", "matching.retry",
        "tutors.read", "walkers.read", "walker_documents.read",
        "occurrences.read", "occurrences.manage",
        "finance.read", "tips.read",
        "alerts.read", "alerts.resolve",
    ],
    "unit_operator": [
        "walks.read", "walks.update_status",
        "tutors.read", "walkers.read",
        "occurrences.read", "alerts.read",
    ],
    "tutor": ["walks.read", "walks.create", "walks.cancel", "tips.read"],
    "walker": ["walks.read", "walks.update_status", "walker_documents.read", "tips.read"],
}

# role string atual (users.role) -> nome do papel RBAC. Default: tutor.
ROLE_MAP = {
    "super_admin": "global_admin",
    "admin": "tenant_admin",
    "walker": "walker",
    "passeador": "walker",
    "tutor": "tutor",
    "cliente": "tutor",
}
# Papéis cujo assignment carrega o tenant_id do usuário (escopo de tenant).
TENANT_SCOPED_ROLES = {"tenant_admin", "tenant_operator", "unit_operator"}


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.utcnow()

    for key, module, action in PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, key, module, action, created_at, updated_at) "
                "VALUES (:id, :key, :m, :a, :t, :t) ON CONFLICT (key) DO NOTHING"
            ),
            {"id": str(uuid4()), "key": key, "m": module, "a": action, "t": now},
        )

    for name, scope, desc in ROLES:
        bind.execute(
            sa.text(
                "INSERT INTO roles (id, name, scope_type, description, created_at, updated_at) "
                "VALUES (:id, :n, :s, :d, :t, :t) ON CONFLICT (name) DO NOTHING"
            ),
            {"id": str(uuid4()), "n": name, "s": scope, "d": desc, "t": now},
        )

    role_ids = {r.name: r.id for r in bind.execute(sa.text("SELECT id, name FROM roles"))}
    perm_ids = {p.key: p.id for p in bind.execute(sa.text("SELECT id, key FROM permissions"))}

    for role_name, perms in ROLE_PERMS.items():
        keys = list(perm_ids.keys()) if perms == "*" else perms
        for key in keys:
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                    "VALUES (:id, :r, :p, :t) ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"id": str(uuid4()), "r": role_ids[role_name], "p": perm_ids[key], "t": now},
            )

    # Mapeia cada usuário existente para um papel (sem duplicar assignments).
    for user in bind.execute(sa.text("SELECT id, role, tenant_id FROM users")):
        target = ROLE_MAP.get((user.role or "").strip().lower(), "tutor")
        role_id = role_ids[target]
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM user_role_assignments "
                "WHERE user_id = :u AND role_id = :r AND revoked_at IS NULL"
            ),
            {"u": user.id, "r": role_id},
        ).first()
        if exists:
            continue
        tenant_id = user.tenant_id if target in TENANT_SCOPED_ROLES else None
        bind.execute(
            sa.text(
                "INSERT INTO user_role_assignments "
                "(id, user_id, role_id, tenant_id, created_by, created_at) "
                "VALUES (:id, :u, :r, :tid, 'rbac_seed', :t)"
            ),
            {"id": str(uuid4()), "u": user.id, "r": role_id, "tid": tenant_id, "t": now},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM user_role_assignments WHERE created_by = 'rbac_seed'"))
    bind.execute(sa.text("DELETE FROM role_permissions"))
    bind.execute(sa.text("DELETE FROM permissions"))
    bind.execute(sa.text("DELETE FROM roles"))
