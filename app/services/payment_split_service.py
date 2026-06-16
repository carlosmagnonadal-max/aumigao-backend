"""Cálculo de split de receita (Sprint 16, Fase A).

Determina como o valor de um pagamento se divide entre a plataforma/tenant
(comissão) e o walker. A comissão vem da config do tenant; na ausência dela,
usa o padrão. Esta fase apenas REGISTRA o split (contábil) — o repasse real ao
walker via gateway é a Fase B.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.tenant_payment_config import (
    DEFAULT_COMMISSION_PERCENT,
    TenantPaymentConfig,
    commission_default_for_plan,
)


def _commission_fallback_for_tenant(db: Session, tenant_id: str | None) -> float:
    """Fallback de comissão quando o tenant ainda não tem TenantPaymentConfig.

    Deriva do TIER do plano do tenant (12/8/5 via commission_default_for_plan),
    NUNCA do legado de 20%. Defensivo: se a tabela/registro de tenant não existir
    (ex.: testes isolados) ou o tenant_id for nulo, cai no fallback de plano
    desconhecido (10%) — coerente com commission_default_for_plan(None).
    """
    plan = None
    if tenant_id:
        try:
            from app.models.tenant import Tenant

            tenant = db.get(Tenant, tenant_id)
            plan = tenant.plan if tenant else None
        except Exception:
            plan = None
    return commission_default_for_plan(plan)


def get_commission_percent(db: Session, tenant_id: str | None) -> float:
    if tenant_id:
        config = (
            db.query(TenantPaymentConfig)
            .filter(
                TenantPaymentConfig.tenant_id == tenant_id,
                TenantPaymentConfig.active.is_(True),
            )
            .first()
        )
        if config and config.commission_percent is not None:
            return float(config.commission_percent)
    # Sem config ativa: deriva do plano do tenant (12/8/5), não do legado de 20%.
    return _commission_fallback_for_tenant(db, tenant_id)


def get_tenant_margin_percent(db: Session, tenant_id: str | None) -> float:
    if tenant_id:
        config = (
            db.query(TenantPaymentConfig)
            .filter(
                TenantPaymentConfig.tenant_id == tenant_id,
                TenantPaymentConfig.active.is_(True),
            )
            .first()
        )
        if config and config.tenant_margin_percent is not None:
            return float(config.tenant_margin_percent)
    return 0.0


def get_plan_discount_percent(db: Session, tenant_id: str | None) -> float:
    """% de desconto de plano que o tenant concede por passeio (0 se sem config)."""
    if tenant_id:
        config = (
            db.query(TenantPaymentConfig)
            .filter(
                TenantPaymentConfig.tenant_id == tenant_id,
                TenantPaymentConfig.active.is_(True),
            )
            .first()
        )
        if config and config.plan_discount_percent is not None:
            return float(config.plan_discount_percent)
    return 0.0


def compute_quote(walk_price: float, plan_discount_percent: float = 0.0) -> dict[str, float]:
    """Cotação por tenant: preço do passeio, desconto de plano e total a pagar.

    Decisão Carlos (2026-06-16): SEM taxa de serviço (R$5 removida). O desconto de
    plano é um % por tenant. total = walk_price - plan_discount.
    """
    walk_price = round(float(walk_price or 0), 2)
    plan_discount_percent = max(0.0, min(100.0, float(plan_discount_percent or 0)))
    plan_discount = round(walk_price * plan_discount_percent / 100.0, 2)
    total = round(walk_price - plan_discount, 2)
    return {
        "walk_price": walk_price,
        "plan_discount_percent": plan_discount_percent,
        "plan_discount": plan_discount,
        "total": total,
    }


def build_quote(db: Session, tenant_id: str | None, walk_price: float) -> dict[str, float]:
    return compute_quote(walk_price, get_plan_discount_percent(db, tenant_id))


def compute_split(amount: float, commission_percent: float, tenant_margin_percent: float = 0.0) -> dict[str, float]:
    amount = round(float(amount or 0), 2)
    commission_percent = max(0.0, min(100.0, float(commission_percent)))
    tenant_margin_percent = max(0.0, float(tenant_margin_percent))
    # Validacao: plataforma + margem nao podem ultrapassar 90%
    if commission_percent + tenant_margin_percent > 90.0:
        tenant_margin_percent = max(0.0, 90.0 - commission_percent)
    platform_amount = round(amount * commission_percent / 100.0, 2)
    tenant_amount = round(amount * tenant_margin_percent / 100.0, 2)
    walker_amount = round(amount - platform_amount - tenant_amount, 2)
    return {
        "commission_percent": commission_percent,
        "tenant_margin_percent": tenant_margin_percent,
        "platform_amount": platform_amount,
        "tenant_amount": tenant_amount,
        "walker_amount": walker_amount,
    }


def walker_percent_from_split(split: dict[str, float]) -> float:
    """Percentual repassado ao walker no gateway, derivado dos amounts do split.

    Fonte única (R2/R10): walker_amount / (platform+tenant+walker). Honra a margem
    do tenant e mantém o repasse no gateway igual ao repasse contábil de compute_split.
    Retorna 0.0 quando o total é zero (sem divisão por zero).
    """
    total = (
        split["platform_amount"]
        + split.get("tenant_amount", 0.0)
        + split["walker_amount"]
    )
    return round(split["walker_amount"] / total * 100.0, 4) if total > 0 else 0.0


def build_payment_split(db: Session, tenant_id: str | None, amount: float) -> dict[str, float]:
    return compute_split(
        amount,
        get_commission_percent(db, tenant_id),
        get_tenant_margin_percent(db, tenant_id),
    )


def get_or_create_payment_config(db: Session, tenant_id: str) -> TenantPaymentConfig:
    config = (
        db.query(TenantPaymentConfig)
        .filter(TenantPaymentConfig.tenant_id == tenant_id)
        .first()
    )
    if not config:
        # Comissão inicial vem do TIER do plano do tenant (white label).
        # Defensivo: se a tabela/registro de tenant não existir (ex.: testes isolados),
        # cai no fallback de plano desconhecido.
        plan = None
        try:
            from app.models.tenant import Tenant

            tenant = db.get(Tenant, tenant_id)
            plan = tenant.plan if tenant else None
        except Exception:
            plan = None
        config = TenantPaymentConfig(
            tenant_id=tenant_id,
            commission_percent=commission_default_for_plan(plan),
        )
        db.add(config)
        db.flush()
    return config


def update_payment_config(
    db: Session,
    tenant_id: str,
    *,
    commission_percent: float | None = None,
    tenant_margin_percent: float | None = None,
    provider: str | None = None,
    split_enabled: bool | None = None,
    actor=None,
) -> TenantPaymentConfig:
    config = get_or_create_payment_config(db, tenant_id)
    before = {
        "commission_percent": config.commission_percent,
        "tenant_margin_percent": getattr(config, "tenant_margin_percent", 0.0),
        "provider": config.provider,
        "split_enabled": config.split_enabled,
    }

    if commission_percent is not None:
        new_commission = max(0.0, min(100.0, float(commission_percent)))
        # Edição manual da comissão = override negociado: protege do default do plano.
        if new_commission != config.commission_percent:
            config.commission_is_custom = True
        config.commission_percent = new_commission
    if tenant_margin_percent is not None:
        config.tenant_margin_percent = max(0.0, float(tenant_margin_percent))
    if provider is not None and provider.strip():
        config.provider = provider.strip()
    if split_enabled is not None:
        config.split_enabled = bool(split_enabled)

    after = {
        "commission_percent": config.commission_percent,
        "tenant_margin_percent": getattr(config, "tenant_margin_percent", 0.0),
        "provider": config.provider,
        "split_enabled": config.split_enabled,
    }
    # Mudança de regra financeira é sensível — auditar (spec §14.3).
    try:
        from app.services.audit_service import record_audit_log

        record_audit_log(
            db,
            action="payment_config.updated",
            entity_type="tenant_payment_config",
            entity_id=tenant_id,
            actor=actor,
            before=before,
            after=after,
            tenant_id=tenant_id,
        )
    except Exception:
        pass

    db.commit()
    db.refresh(config)
    return config
