"""Configuracoes administrativas persistidas no banco (key-value JSON).

Substitui os dicts em memoria REFERRAL_PROGRAM_SETTINGS e
WALKER_PROGRAM_SETTINGS que sumiam a cada deploy do Railway.

Fase 3 T1: adiciona tenant_id para suporte a configuracoes per-tenant.
Linhas com tenant_id IS NULL = configuracao global/fallback.

NOTA: mantemos `key` como PK para retrocompatibilidade com linhas globais
existentes. Para suporte multi-tenant, o servico usa queries por (key, tenant_id)
em vez de db.get(AppSetting, key).
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AppSetting(Base):
    """Armazena configuracoes de programa (referral, walker, etc.) como JSON.

    Suporta escopo por tenant (tenant_id) com fallback global (tenant_id IS NULL).
    A PK eh uma string UUID para permitir multiplas linhas com o mesmo `key`
    em escopos distintos (global vs por tenant).
    O unique constraint (tenant_id, key) garante unicidade por escopo.
    """

    __tablename__ = "app_settings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_app_settings_tenant_key"),
    )

    # PK eh UUID para suportar multiplas linhas com o mesmo key em escopos distintos.
    # Retrocompatibilidade: a migration 0023 adiciona tenant_id; linhas existentes
    # ficam com tenant_id NULL (escopo global) e key ainda funciona como identificador.
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True, default=None)
    value_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
