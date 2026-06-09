"""spec §13 — tabela upload_files (registro de documentos/KYC base)

Cria a tabela upload_files (nova, vazia). Aditivo e reversível.

Revision ID: 0011_upload_files
Revises: 0010_payment_split
Create Date: 2026-06-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0011_upload_files"
down_revision: Union[str, None] = "0010_payment_split"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "upload_files",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("owner_id", sa.String(), nullable=True),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("document_type", sa.String(), nullable=True),
        sa.Column("storage_path", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_upload_files_tenant_id", "upload_files", ["tenant_id"])
    op.create_index("ix_upload_files_owner_id", "upload_files", ["owner_id"])
    op.create_index("ix_upload_files_context", "upload_files", ["context"])
    op.create_index("ix_upload_files_document_type", "upload_files", ["document_type"])
    op.create_index("ix_upload_files_created_at", "upload_files", ["created_at"])


def downgrade() -> None:
    op.drop_table("upload_files")
