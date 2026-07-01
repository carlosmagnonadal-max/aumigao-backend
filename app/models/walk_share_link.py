"""Token de compartilhamento público de passeio ao vivo (growth loop cunha 1).

Um WalkShareLink concede acesso de LEITURA público (sem auth) ao passeio ao vivo,
via token não-adivinhável. Revogável e efêmero (expires_at). Sem PII: apenas ids
e timestamps. Não protegido por RLS de usuário — o token É a capability e o read
público usa global_scope_session; a escrita é feita por endpoint autenticado que
valida a posse do passeio.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkShareLink(Base):
    __tablename__ = "walk_share_links"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    walk_id: Mapped[str] = mapped_column(String, ForeignKey("walks.id"), index=True, nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String, ForeignKey("tenants.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
