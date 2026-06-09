"""Registro de arquivos enviados (spec §13 — base para gestão de documentos/KYC).

Cada upload (documento de candidatura, foto de pet, foto de finalização) gera um
registro com metadados — rastreabilidade de QUEM enviou O QUÊ, QUANDO e ONDE.
É a base do KYC da Fase B do financeiro (cadastrar o walker como recebedor exige
os documentos rastreados). Não guarda o conteúdo, só a referência/metadados.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UploadFile(Base):
    __tablename__ = "upload_files"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    context: Mapped[str] = mapped_column(String, index=True)  # partner_application | pet | walk_completion
    document_type: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    storage_path: Mapped[str] = mapped_column(String)
    mime_type: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
