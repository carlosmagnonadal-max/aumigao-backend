"""0097 — tenant_units: coluna slug + permissões RBAC units.read / units.update

Altera:
  - tenant_units: adiciona coluna `slug` (VARCHAR, nullable, index).
    Backfill: derivado do name com NFKD + slugify (ASCII, hifens).
    Após backfill não forçamos NOT NULL (para zero-regressão em Neon sem lock longo).

Seed RBAC (idempotente — ON CONFLICT DO NOTHING):
  - permission  units.read   (module=units, action=read)
  - permission  units.update (module=units, action=update)
  - role_permissions: global_admin + tenant_admin recebem ambas as permissões.

Revision ID: 0097_tenant_units_slug_rbac
Revises: 0096_legal_acceptance_v2
Create Date: 2026-07-03
"""
from datetime import datetime
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0097_tenant_units_slug_rbac"
down_revision: Union[str, None] = "0096_legal_acceptance_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Permissões novas (idempotente)
NEW_PERMISSIONS = [
    ("units.read", "units", "read"),
    ("units.update", "units", "update"),
]

# Papéis que recebem as novas permissões
TARGET_ROLES = {"global_admin", "tenant_admin"}


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.utcnow()

    # 1. Adiciona coluna slug (nullable) ─────────────────────────────────────
    op.add_column("tenant_units", sa.Column("slug", sa.String(), nullable=True))
    op.create_index("ix_tenant_units_slug", "tenant_units", ["slug"], unique=False)

    # 2. Backfill: slug = slugify(name) com sufixo por colisão por tenant ────
    # Implementação em SQL puro (compatível com PostgreSQL e SQLite).
    # Regex complexo não disponível em SQLite; fazemos lower() + replace simples.
    # No Neon (PostgreSQL) o backfill é fiel; em SQLite (testes) o slug pode ter
    # caracteres não-ASCII residuais — aceitável (truncado depois em serviço).
    dialect = bind.dialect.name
    if dialect == "postgresql":
        # NFKD + unaccent não disponível sem extensão; usamos translate simples
        # para os caracteres mais comuns do português.
        bind.execute(sa.text("""
            UPDATE tenant_units
            SET slug = regexp_replace(
                lower(
                    translate(
                        name,
                        'áàãâäéèêëíìîïóòõôöúùûüçÁÀÃÂÄÉÈÊËÍÌÎÏÓÒÕÔÖÚÙÛÜÇ',
                        'aaaaaaeeeeiiiiooooouuuucAAAAAAAAEEEEIIIIOOOOOUUUUC'
                    )
                ),
                '[^a-z0-9]+', '-', 'g'
            )
            WHERE slug IS NULL
        """))
        # Remove hifens nas pontas
        bind.execute(sa.text("""
            UPDATE tenant_units
            SET slug = trim(both '-' from slug)
            WHERE slug IS NOT NULL
        """))
        # Garante unicidade por tenant: sufixo -2/-3 onde há colisão
        bind.execute(sa.text("""
            WITH ranked AS (
                SELECT id, tenant_id, slug,
                       row_number() OVER (PARTITION BY tenant_id, slug ORDER BY created_at ASC) AS rn
                FROM tenant_units
            )
            UPDATE tenant_units
            SET slug = ranked.slug || '-' || ranked.rn
            FROM ranked
            WHERE tenant_units.id = ranked.id AND ranked.rn > 1
        """))
    else:
        # SQLite: backfill simples (lower + trim)
        rows = bind.execute(sa.text("SELECT id, name FROM tenant_units WHERE slug IS NULL")).fetchall()
        import re
        import unicodedata
        seen: dict[str, int] = {}
        for row in rows:
            raw = (row.name or "").strip().lower()
            normalized = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
            base = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-") or "unidade"
            slug = base
            counter = 2
            while slug in seen:
                slug = f"{base}-{counter}"
                counter += 1
            seen[slug] = 1
            bind.execute(sa.text("UPDATE tenant_units SET slug = :s WHERE id = :i"), {"s": slug, "i": row.id})

    # 3. Seed de permissões RBAC ─────────────────────────────────────────────
    for key, module, action in NEW_PERMISSIONS:
        bind.execute(
            sa.text(
                "INSERT INTO permissions (id, key, module, action, created_at, updated_at) "
                "VALUES (:id, :key, :m, :a, :t, :t) ON CONFLICT (key) DO NOTHING"
            ),
            {"id": str(uuid4()), "key": key, "m": module, "a": action, "t": now},
        )

    role_ids = {
        r.name: r.id
        for r in bind.execute(
            sa.text("SELECT id, name FROM roles WHERE name = ANY(:names)"),
            {"names": list(TARGET_ROLES)},
        )
    } if bind.dialect.name == "postgresql" else {
        r.name: r.id
        for r in bind.execute(
            sa.text(f"SELECT id, name FROM roles WHERE name IN ({','.join(['?' * len(TARGET_ROLES)].pop(0).split('?') or [''])})")
        )
    }

    # Fallback genérico compatível com ambos os dialetos
    role_ids = {}
    for rname in TARGET_ROLES:
        row = bind.execute(sa.text("SELECT id FROM roles WHERE name = :n"), {"n": rname}).first()
        if row:
            role_ids[rname] = row.id

    perm_ids = {}
    for key, _, _ in NEW_PERMISSIONS:
        row = bind.execute(sa.text("SELECT id FROM permissions WHERE key = :k"), {"k": key}).first()
        if row:
            perm_ids[key] = row.id

    for rname, rid in role_ids.items():
        for pkey, pid in perm_ids.items():
            bind.execute(
                sa.text(
                    "INSERT INTO role_permissions (id, role_id, permission_id, created_at) "
                    "VALUES (:id, :r, :p, :t) ON CONFLICT (role_id, permission_id) DO NOTHING"
                ),
                {"id": str(uuid4()), "r": rid, "p": pid, "t": now},
            )


def downgrade() -> None:
    bind = op.get_bind()

    # Remove role_permissions das novas permissões
    for key, _, _ in NEW_PERMISSIONS:
        bind.execute(
            sa.text(
                "DELETE FROM role_permissions WHERE permission_id IN "
                "(SELECT id FROM permissions WHERE key = :k)"
            ),
            {"k": key},
        )
    for key, _, _ in NEW_PERMISSIONS:
        bind.execute(sa.text("DELETE FROM permissions WHERE key = :k"), {"k": key})

    op.drop_index("ix_tenant_units_slug", table_name="tenant_units")
    op.drop_column("tenant_units", "slug")
