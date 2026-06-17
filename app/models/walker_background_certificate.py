"""Certidao de antecedentes do passeador (Background Check Fase 0).

Uma linha por certidao emitida pelo passeador (1 por cert_type/issuer_uf).
O passeador emite a certidao oficial GRATUITA, faz upload do PDF e digita o
numero; o admin valida semi-manualmente (link da pagina oficial). Tudo atras
da flag de tenant `background_checks` (default-OFF) -> ZERO efeito em producao
ate ligarem.

Tipos de certidao (cert_type):
- "pf"  -> Antecedentes da Policia Federal (OBRIGATORIA)
- "tj"  -> Distribuicao Criminal do TJ estadual do domicilio (OBRIGATORIA)
- "trf" -> Distribuicao Criminal da Justica Federal (COMPLEMENTAR/opcional)
- "tse" -> Crimes Eleitorais (COMPLEMENTAR/opcional)

Spec: docs/plano-background-check-fase0-2026-06-16.md
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WalkerBackgroundCertificate(Base):
    __tablename__ = "walker_background_certificates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    walker_profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("walker_profiles.id"), index=True, nullable=False
    )
    # "pf" | "tj" | "trf" | "tse"
    cert_type: Mapped[str] = mapped_column(String, nullable=False)
    # UF emissora (relevante para tj/trf). None para pf/tse (federais nacionais).
    issuer_uf: Mapped[str | None] = mapped_column(String, nullable=True)
    document_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Numero da certidao digitado pelo passeador (validacao semi-manual).
    cert_number: Mapped[str | None] = mapped_column(String, nullable=True)
    # pending | validated | rejected | expired
    status: Mapped[str] = mapped_column(String, default="pending", server_default="pending", nullable=False)
    validated_by_admin_id: Mapped[str | None] = mapped_column(String, nullable=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
