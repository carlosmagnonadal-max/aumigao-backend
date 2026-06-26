from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid4())


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    plan: Mapped[str] = mapped_column(String, nullable=False, default="starter")
    legal_name: Mapped[str | None] = mapped_column(String, nullable=True)
    document_number: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    asaas_customer_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Projeto B: motivo da suspensão — "billing" (inadimplência, reativável por pagamento)
    # ou "manual" (super_admin, NÃO reativa automático). NULL quando não suspenso.
    suspended_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Fase 1 Passo 1 (migration 0048) ──────────────────────────────────────
    # Override manual de acesso à rede: True=força ativo, False=força inativo, None=segue regra de plano.
    network_access_override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Addon de rede para plano Business: True=rede habilitada, False=não (default).
    # server_default "false" — padrão do projeto para Boolean=False.
    network_access_addon: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=sa.text("false")
    )
    # ── F3.2 (migration 0052) ─────────────────────────────────────────────────
    # Requisitos extras que ESTE tenant exige do passeador (além do background baseline).
    # NULL/[] = sem gate (comportamento legado). Ex.: ["Curso de primeiros socorros", "Entrevista"].
    walker_extra_requirements: Mapped[list | None] = mapped_column(sa.JSON, nullable=True)

    branding: Mapped["TenantBranding | None"] = relationship(back_populates="tenant", uselist=False, cascade="all, delete-orphan")
    settings: Mapped["TenantSettings | None"] = relationship(back_populates="tenant", uselist=False, cascade="all, delete-orphan")
    onboarding: Mapped["TenantOnboarding | None"] = relationship(back_populates="tenant", uselist=False, cascade="all, delete-orphan")
    features: Mapped[list["TenantFeature"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    units: Mapped[list["TenantUnit"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class TenantBranding(Base):
    __tablename__ = "tenant_branding"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    app_name: Mapped[str | None] = mapped_column(String, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    icon_url: Mapped[str | None] = mapped_column(String, nullable=True)
    splash_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String, nullable=True)
    secondary_color: Mapped[str | None] = mapped_column(String, nullable=True)
    accent_color: Mapped[str | None] = mapped_column(String, nullable=True)
    powered_by_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Incrementa a cada publicação; o cliente (mobile/admin) usa para invalidar cache (spec §9.4).
    published_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="branding")


class TenantFeature(Base):
    __tablename__ = "tenant_features"
    __table_args__ = (UniqueConstraint("tenant_id", "feature_key", name="uq_tenant_features_tenant_key"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    feature_key: Mapped[str] = mapped_column(String, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    limit_value: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="features")


class TenantSettings(Base):
    __tablename__ = "tenant_settings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="America/Bahia")
    support_email: Mapped[str | None] = mapped_column(String, nullable=True)
    support_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String, nullable=True)
    settings_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Background check provider plugavel por tenant (migration 0040).
    # Valores validos: "manual" | "flagcheck" | "idwall" | "serpro".
    # Default "manual" — comportamento identico ao anterior (zero regressao).
    background_check_provider: Mapped[str] = mapped_column(
        String, nullable=False, default="manual", server_default="manual"
    )
    # Credenciais/config do provedor pago (JSON, nullable).
    # TODO: cifrar com Fernet/KMS antes de habilitar provedor pago em producao.
    background_check_provider_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="settings")


class TenantUnit(Base):
    __tablename__ = "tenant_units"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    city: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tenant: Mapped[Tenant] = relationship(back_populates="units")
