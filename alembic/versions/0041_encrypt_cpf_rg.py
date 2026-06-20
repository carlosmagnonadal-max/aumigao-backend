"""Cifragem em repouso de CPF e RG (migration 0041).

Adiciona colunas de blind-index em tutor_profiles e walker_profiles para suportar
busca/unicidade de CPF sem expor o valor em texto puro.

Colunas adicionadas:
- tutor_profiles.cpf_bidx   (String, nullable, index)
- walker_profiles.cpf_bidx  (String, nullable, index)

Os valores existentes de CPF (e RG para walker) são:
- Se vazio           → pulado
- Se já cifrado      → só atualiza o bidx (idempotente — Fernet detectável)
- Se texto puro      → cifra o valor E preenche o bidx

DOWNGRADE: remove apenas as colunas cpf_bidx.
ATENÇÃO: os valores cifrados NÃO são revertidos a texto puro no downgrade.
Para reverter a cifragem seria necessário um script específico com a PII_ENCRYPTION_KEY.

Revision ID: 0041_encrypt_cpf_rg
Revises: 0040_bg_check_provider
Create Date: 2026-06-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0041_encrypt_cpf_rg"
down_revision: Union[str, None] = "0040_bg_check_provider"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _has_index(table: str, index_name: str) -> bool:
    return index_name in {
        idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(table)
    }


def upgrade() -> None:
    # Garante que PII_ENCRYPTION_KEY está disponível antes de qualquer operação.
    # Importar DEPOIS do check de ambiente para dar erro legível.
    from app.core.pii_crypto import blind_index, encrypt, is_encrypted  # noqa: PLC0415

    conn = op.get_bind()

    # --- tutor_profiles ---
    if _has_table("tutor_profiles"):
        if not _has_column("tutor_profiles", "cpf_bidx"):
            op.add_column(
                "tutor_profiles",
                sa.Column("cpf_bidx", sa.String(), nullable=True),
            )
        if not _has_index("tutor_profiles", "ix_tutor_profiles_cpf_bidx"):
            op.create_index(
                "ix_tutor_profiles_cpf_bidx",
                "tutor_profiles",
                ["cpf_bidx"],
            )

        # Backfill tutor CPF
        rows = conn.execute(
            sa.text("SELECT id, cpf FROM tutor_profiles")
        ).fetchall()
        for row_id, raw_cpf in rows:
            if not raw_cpf:
                continue
            if is_encrypted(raw_cpf):
                # Já cifrado — só garante o bidx (que pode estar NULL de rodada anterior).
                # Para decifrar usaríamos decrypt(), mas para o bidx precisamos do plaintext.
                # Como só temos o cifrado, precisamos decifrar para recalcular o bidx.
                from app.core.pii_crypto import decrypt  # noqa: PLC0415
                plaintext = decrypt(raw_cpf)
                bidx = blind_index(plaintext)
                conn.execute(
                    sa.text(
                        "UPDATE tutor_profiles SET cpf_bidx = :bidx WHERE id = :id"
                    ),
                    {"bidx": bidx, "id": row_id},
                )
            else:
                # Texto puro — cifrar e calcular bidx.
                encrypted_cpf = encrypt(raw_cpf)
                bidx = blind_index(raw_cpf)
                conn.execute(
                    sa.text(
                        "UPDATE tutor_profiles SET cpf = :cpf, cpf_bidx = :bidx WHERE id = :id"
                    ),
                    {"cpf": encrypted_cpf, "bidx": bidx, "id": row_id},
                )

    # --- walker_profiles ---
    if _has_table("walker_profiles"):
        if not _has_column("walker_profiles", "cpf_bidx"):
            op.add_column(
                "walker_profiles",
                sa.Column("cpf_bidx", sa.String(), nullable=True),
            )
        if not _has_index("walker_profiles", "ix_walker_profiles_cpf_bidx"):
            op.create_index(
                "ix_walker_profiles_cpf_bidx",
                "walker_profiles",
                ["cpf_bidx"],
            )

        # Backfill walker CPF e RG
        rows = conn.execute(
            sa.text("SELECT id, cpf, rg FROM walker_profiles")
        ).fetchall()
        for row_id, raw_cpf, raw_rg in rows:
            updates: dict = {}

            # CPF
            if raw_cpf:
                if is_encrypted(raw_cpf):
                    from app.core.pii_crypto import decrypt  # noqa: PLC0415
                    plaintext_cpf = decrypt(raw_cpf)
                    updates["cpf_bidx"] = blind_index(plaintext_cpf)
                else:
                    updates["cpf"] = encrypt(raw_cpf)
                    updates["cpf_bidx"] = blind_index(raw_cpf)

            # RG
            if raw_rg and not is_encrypted(raw_rg):
                updates["rg"] = encrypt(raw_rg)

            if updates:
                set_clause = ", ".join(f"{k} = :{k}" for k in updates)
                updates["id"] = row_id
                conn.execute(
                    sa.text(
                        f"UPDATE walker_profiles SET {set_clause} WHERE id = :id"
                    ),
                    updates,
                )


def downgrade() -> None:
    # Remove apenas as colunas de blind-index.
    # Os valores cifrados NÃO são revertidos a texto puro.
    # Para reverter a cifragem, use um script separado com PII_ENCRYPTION_KEY.

    if _has_table("walker_profiles"):
        if _has_index("walker_profiles", "ix_walker_profiles_cpf_bidx"):
            op.drop_index("ix_walker_profiles_cpf_bidx", table_name="walker_profiles")
        if _has_column("walker_profiles", "cpf_bidx"):
            op.drop_column("walker_profiles", "cpf_bidx")

    if _has_table("tutor_profiles"):
        if _has_index("tutor_profiles", "ix_tutor_profiles_cpf_bidx"):
            op.drop_index("ix_tutor_profiles_cpf_bidx", table_name="tutor_profiles")
        if _has_column("tutor_profiles", "cpf_bidx"):
            op.drop_column("tutor_profiles", "cpf_bidx")
