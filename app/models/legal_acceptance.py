from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class LegalAcceptance(Base):
    __tablename__ = "legal_acceptances"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    user_role: Mapped[str] = mapped_column(String, index=True)
    # tenant_id NULL = aceite de PLATAFORMA (1x, re-aceite se versao mudar).
    # tenant_id preenchido = aceite POR TENANT (Modelo B: primeiro acesso a cada
    # estabelecimento; a relacao do passeio e com o tenant).
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    terms_version: Mapped[str] = mapped_column(String, default="")
    privacy_version: Mapped[str] = mapped_column(String, default="")
    cancellation_version: Mapped[str] = mapped_column(String, default="")
    lgpd_version: Mapped[str] = mapped_column(String, default="")
    geolocation_version: Mapped[str] = mapped_column(String, default="")
    accepted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TenantLegalDocument(Base):
    """Documento legal que o tenant configura no admin a partir de MODELOS BASE (Fase 2).

    Modelo B: a relacao do passeio e com o ESTABELECIMENTO. Cada tenant pode adequar os
    proprios textos (contrato tutor, politica de cancelamento, termo do passeador) a
    partir de modelos base fornecidos pela plataforma SEM responsabilizacao dela — cada
    estabelecimento deve validar com seu advogado.

    Versionamento simples por is_active: no maximo UMA versao ATIVA por (tenant_id,
    doc_type). Ao gravar uma nova versao custom, a anterior e desativada (mantida para
    historico/auditoria). Quando o tenant NUNCA customizou um doc_type, nao ha linha
    aqui e a leitura cai no MODELO BASE (is_custom=false, version="base-2026-07").
    """

    __tablename__ = "tenant_legal_documents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    doc_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
