"""Configuração financeira por tenant (Sprint 16 — White Label gateway-agnóstico).

Cada tenant define seu gateway de pagamento e a comissão que retém de cada
transação. O Aumigão usa Asaas; outros tenants podem usar outros gateways.
Credenciais NÃO ficam aqui em claro — serão referenciadas por secret/env (Fase B).

Pricing v2 (2026-06-24):
  2 planos canônicos: Pro e Enterprise.
  Legado: starter/business → Pro; enterprise → Enterprise.
  Take-rate PRÓPRIO:  Pro 10% / Enterprise 5%.
  Take-rate de REDE:  Pro 18% / Enterprise 10%.
  Controlado por PRICING_V2_ENABLED (default False → legado ativo, sem regressão).
"""
import logging
import os
from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

logger = logging.getLogger(__name__)

# Default da COLUNA commission_percent para novos registros SEM valor explícito.
# Antes era 20.0 — legado MORTO da migração 3→2 planos (nenhum plano vigente cobra
# 20%). Neutralizado para 10.0, o piso sensato do plano Pro (take-rate próprio).
# Todo caminho de criação real de TenantPaymentConfig passa `commission_percent`
# derivado do plano (commission_default_for_plan); este default só cobre linhas
# criadas sem plano resolvido — usar o piso Pro evita cobrar 20% fantasma.
DEFAULT_COMMISSION_PERCENT = 10.0

# ── Pricing v1 (legado) ─────────────────────────────────────────────────────
# 3 planos: starter 12% / business 8% / enterprise 5%.
# Mantido para PRICING_V2_ENABLED=False (zero-regressão).
PLAN_COMMISSION_DEFAULTS = {"starter": 12.0, "business": 8.0, "enterprise": 5.0}
PLAN_COMMISSION_FALLBACK = 10.0

# ── Pricing v2 (2 planos + take-rate de REDE) ───────────────────────────────
# Planos canônicos.
TENANT_PLAN_PRO = "pro"
TENANT_PLAN_ENTERPRISE_V2 = "enterprise"

# Mapeamento de chaves legadas → canônicas v2.
_LEGACY_PLAN_MAP: dict[str, str] = {
    "starter": TENANT_PLAN_PRO,
    "business": TENANT_PLAN_PRO,
    "enterprise": TENANT_PLAN_ENTERPRISE_V2,
    # Chave canônica já é v2 → passa direto.
    "pro": TENANT_PLAN_PRO,
}

# Take-rate PRÓPRIO por plano v2 (tenant põe a mão de obra).
PLAN_COMMISSION_DEFAULTS_V2: dict[str, float] = {
    TENANT_PLAN_PRO: 10.0,
    TENANT_PLAN_ENTERPRISE_V2: 5.0,
}

# Take-rate de REDE por plano v2 (Rede Aumigão fornece o passeador).
PLAN_NETWORK_COMMISSION_V2: dict[str, float] = {
    TENANT_PLAN_PRO: 18.0,
    TENANT_PLAN_ENTERPRISE_V2: 10.0,
}

PLAN_COMMISSION_FALLBACK_V2 = 10.0
PLAN_NETWORK_COMMISSION_FALLBACK_V2 = 18.0

# Flag de ambiente: PRICING_V2_ENABLED=true liga os novos valores.
# Default False → comportamento 100% idêntico ao v1 (zero-regressão).
_PRICING_V2_ENABLED: bool = os.getenv("PRICING_V2_ENABLED", "false").lower() in {"1", "true", "yes"}


def canonical_plan_v2(plan: str | None) -> str:
    """Mapeia chave legada (starter/business/enterprise) → canônica v2 (pro/enterprise).

    Não altera o banco — usado apenas para resolução de take-rate.
    Chaves desconhecidas retornam TENANT_PLAN_PRO (plano padrão).
    """
    normalized = (plan or "").strip().lower()
    return _LEGACY_PLAN_MAP.get(normalized, TENANT_PLAN_PRO)


def commission_default_for_plan(plan: str | None) -> float:
    """Take-rate PRÓPRIO por plano.

    PRICING_V2_ENABLED=False (default): legado 12/8/5.
    PRICING_V2_ENABLED=True:            v2 Pro 10% / Enterprise 5%.

    Fallback (money-fix P1): antes, QUALQUER plano fora da tabela retornava 10%
    silencioso — inclusive um plano com nome desconhecido (typo, plano renomeado,
    dado corrompido), o que cobra a esmo. Decisão explícita agora:

      - plano AUSENTE (None/"" — tenant sem plano definido): usa o piso do plano
        padrão (Pro 10% v2 / fallback legado 10%). É o caso benigno de "ainda não
        configurado" e manter uma comissão default é aceitável.
      - plano PRESENTE mas DESCONHECIDO (string não vazia fora da tabela): NÃO
        cobrar às cegas. Loga ERRO e retorna 0.0 (não cobrar sem config reconhecida).
        O caller de billing deve tratar 0% como "sem comissão" e a configuração
        correta deve ser feita explicitamente no admin.
    """
    raw = (plan or "").strip()
    normalized = raw.lower()
    plan_absent = raw == ""

    if _PRICING_V2_ENABLED:
        table = PLAN_COMMISSION_DEFAULTS_V2
        canon = canonical_plan_v2(normalized)
        # canonical_plan_v2 mapeia desconhecido → 'pro'; então detecte "desconhecido"
        # ANTES: só é conhecido se estava no mapa legado→canônico.
        if plan_absent:
            return table.get(TENANT_PLAN_PRO, PLAN_COMMISSION_FALLBACK_V2)
        if normalized not in _LEGACY_PLAN_MAP:
            logger.error(
                "commission_default_for_plan: plano DESCONHECIDO %r (v2) — retornando 0%% "
                "(nao cobrar sem config reconhecida). Configure a comissao no admin.",
                raw,
            )
            return 0.0
        return table.get(canon, PLAN_COMMISSION_FALLBACK_V2)

    # v1 (legado)
    if plan_absent:
        return PLAN_COMMISSION_FALLBACK
    if normalized in PLAN_COMMISSION_DEFAULTS:
        return PLAN_COMMISSION_DEFAULTS[normalized]
    # Plano canônico v2 (pro/enterprise) rodando com PRICING_V2 desligado: ainda é
    # um plano VÁLIDO, não um nome bogus. Usa a comissão própria v2 (pro 10/ent 5)
    # em vez de tratar como desconhecido. Antes deste money-fix caía no fallback 10%.
    if normalized in _LEGACY_PLAN_MAP:
        canon = canonical_plan_v2(normalized)
        return PLAN_COMMISSION_DEFAULTS_V2.get(canon, PLAN_COMMISSION_FALLBACK)
    # Genuinamente desconhecido (typo, plano renomeado): não cobrar às cegas.
    logger.error(
        "commission_default_for_plan: plano DESCONHECIDO %r (v1) — retornando 0%% "
        "(nao cobrar sem config reconhecida). Configure a comissao no admin.",
        raw,
    )
    return 0.0


def network_commission_default_for_plan(plan: str | None) -> float:
    """Take-rate de REDE por plano (Rede Aumigão fornece o passeador).

    Sempre usa a tabela v2 (18/10) pois a Rede é um conceito novo.
    Quando PRICING_V2_ENABLED=False o chamador decide se usa este valor.
    """
    canon = canonical_plan_v2(plan)
    return PLAN_NETWORK_COMMISSION_V2.get(canon, PLAN_NETWORK_COMMISSION_FALLBACK_V2)


class TenantPaymentConfig(Base):
    __tablename__ = "tenant_payment_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(
        String, ForeignKey("tenants.id"), nullable=False, unique=True, index=True
    )
    provider: Mapped[str] = mapped_column(String, default="asaas")
    # % do valor do passeio que a PLATAFORMA retém (comissão operadora — só super_admin altera).
    commission_percent: Mapped[float] = mapped_column(Float, default=DEFAULT_COMMISSION_PERCENT)
    # % adicional que o TENANT retém sobre o restante (margem do operador white-label).
    # Default 0: resultado idêntico ao comportamento anterior.
    tenant_margin_percent: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    # % de desconto que o tenant concede por passeio aos tutores do seu plano
    # recorrente (configurável no admin). Default 0: sem desconto.
    plan_discount_percent: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    # Quando True, a comissão foi negociada/editada à mão (ex.: Fundador/sócio 0%) e
    # NÃO é sobrescrita pelo default do plano (backfill ou mudança de plano).
    commission_is_custom: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # Quando True, o split é executado no gateway (walker recebe direto — Fase B).
    split_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
