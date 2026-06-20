"""Backfill: cifra CPF/RG existentes + preenche cpf_bidx (migration 0042 — DADOS).

Parte 2 de 2 do achado #6. Esta migration MUTA dados (cifra os valores em
texto puro). Deve rodar SOMENTE DEPOIS do deploy do código novo (TypeDecorator
EncryptedString com decrypt tolerante), senão o código antigo no ar leria a
cifra como se fosse o CPF.

Para cada linha de tutor_profiles e walker_profiles:
- cpf/rg vazio        → pulado
- cpf/rg já cifrado   → só (re)calcula o cpf_bidx (idempotente)
- cpf/rg texto puro   → cifra o valor E preenche o cpf_bidx

Requer PII_ENCRYPTION_KEY no ambiente (mesma chave do Cloud Run).

DOWNGRADE: no-op. Os valores cifrados NÃO são revertidos a texto puro aqui —
para reverter use um script dedicado com a PII_ENCRYPTION_KEY (decrypt).

Revision ID: 0042_backfill_encrypt_cpf_rg
Revises: 0041_encrypt_cpf_rg
Create Date: 2026-06-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_backfill_encrypt_cpf_rg"
down_revision: Union[str, None] = "0041_encrypt_cpf_rg"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    # Import tardio: dá erro legível de PII_ENCRYPTION_KEY ausente no momento do uso.
    from app.core.pii_crypto import blind_index, decrypt, encrypt, is_encrypted  # noqa: PLC0415

    conn = op.get_bind()

    # --- tutor_profiles: cpf ---
    if _has_table("tutor_profiles"):
        rows = conn.execute(sa.text("SELECT id, cpf FROM tutor_profiles")).fetchall()
        for row_id, raw_cpf in rows:
            if not raw_cpf:
                continue
            if is_encrypted(raw_cpf):
                # Já cifrado — garante apenas o bidx (recalcula a partir do plaintext).
                conn.execute(
                    sa.text("UPDATE tutor_profiles SET cpf_bidx = :bidx WHERE id = :id"),
                    {"bidx": blind_index(decrypt(raw_cpf)), "id": row_id},
                )
            else:
                conn.execute(
                    sa.text("UPDATE tutor_profiles SET cpf = :cpf, cpf_bidx = :bidx WHERE id = :id"),
                    {"cpf": encrypt(raw_cpf), "bidx": blind_index(raw_cpf), "id": row_id},
                )

    # --- walker_profiles: cpf + rg ---
    if _has_table("walker_profiles"):
        rows = conn.execute(sa.text("SELECT id, cpf, rg FROM walker_profiles")).fetchall()
        for row_id, raw_cpf, raw_rg in rows:
            updates: dict = {}
            if raw_cpf:
                if is_encrypted(raw_cpf):
                    updates["cpf_bidx"] = blind_index(decrypt(raw_cpf))
                else:
                    updates["cpf"] = encrypt(raw_cpf)
                    updates["cpf_bidx"] = blind_index(raw_cpf)
            if raw_rg and not is_encrypted(raw_rg):
                updates["rg"] = encrypt(raw_rg)
            if updates:
                set_clause = ", ".join(f"{k} = :{k}" for k in updates)
                updates["id"] = row_id
                conn.execute(
                    sa.text(f"UPDATE walker_profiles SET {set_clause} WHERE id = :id"),
                    updates,
                )


def downgrade() -> None:
    # No-op: a cifragem não é revertida automaticamente (precisa da chave + script).
    pass
