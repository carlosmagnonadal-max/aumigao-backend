"""Token de compartilhamento público do perfil do pet (Fase 4 — LGPD).

Um PetShareLink concede acesso de LEITURA público (sem auth) ao perfil sanitizado
do pet, via token não-adivinhável. Revogável e efêmero (expires_at 30 dias).

LGPD: consent_at registra o consentimento explícito do tutor de que dados de saúde
do pet ficarão visíveis a quem tiver o link. Sem PII do tutor no payload público.
O read público usa global_scope_session; a escrita é feita por endpoint autenticado
que valida a posse do pet.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PetShareLink(Base):
    __tablename__ = "pet_share_links"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    pet_id: Mapped[str] = mapped_column(String, ForeignKey("pets.id"), index=True, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    consent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
