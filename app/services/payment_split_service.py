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
    return DEFAULT_COMMISSION_PERCENT


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
