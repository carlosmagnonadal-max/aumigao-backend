"""Cálculo de split de receita (Sprint 16, Fase A).

Determina como o valor de um pagamento se divide entre a plataforma/tenant
(comissão) e o walker. A comissão vem da config do tenant; na ausência dela,
usa o padrão. Esta fase apenas REGISTRA o split (contábil) — o repasse real ao
walker via gateway é a Fase B.

Pricing v2 (2026-06-24):
  resolve_network_take_rate() retorna o take-rate de REDE do plano (Pro 18% /
  Enterprise 10%). Chamado quando o sinal "passeio de rede" for confirmado:
  is_network_walk(db, tenant_id, walker_id) verifica TenantWalkerAccess.access_type
  ∈ {"shared_network", "tenant_exclusive"} com status=active.
  get_commission_percent_for_walk() encapsula a lógica completa (rede vs próprio).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.tenant_payment_config import (
    DEFAULT_COMMISSION_PERCENT,
    TenantPaymentConfig,
    commission_default_for_plan,
    network_commission_default_for_plan,
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


def get_commission_percent(
    db: Session,
    tenant_id: str | None,
    *,
    walker_id: str | None = None,
) -> float:
    """Retorna a comissão aplicável para o par (tenant, walker).

    Precedência (Fase 1 Passo 4 §D):
      1. TenantWalkerAccess.commission_percent para este par específico (quando
         tanto tenant_id quanto walker_id são fornecidos e existe um registro
         ativo com commission_percent não-nulo).
      2. TenantPaymentConfig.commission_percent ativo do tenant.
      3. Fallback: default do plano do tenant (12/8/5/10%).

    Quando walker_id é None, o comportamento é IDÊNTICO ao original (apenas
    níveis 2 e 3 são verificados) — zero-regressão.
    """
    # Nível 1: comissão negociada por par tenant+walker (Fase 1 Passo 4 §D).
    if tenant_id and walker_id:
        from app.models.tenant_walker_access import TenantWalkerAccess
        twa = (
            db.query(TenantWalkerAccess)
            .filter(
                TenantWalkerAccess.tenant_id == tenant_id,
                TenantWalkerAccess.walker_user_id == walker_id,
                TenantWalkerAccess.status == "active",
            )
            .first()
        )
        if twa is not None and twa.commission_percent is not None:
            return float(twa.commission_percent)

    # Nível 2: config ativa do tenant.
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

    # Nível 3: fallback por plano.
    return _commission_fallback_for_tenant(db, tenant_id)


# ── Pricing v2: take-rate de REDE ───────────────────────────────────────────

def _tenant_plan(db: Session, tenant_id: str | None) -> str | None:
    """Retorna o plano do tenant de forma defensiva (None se não encontrado)."""
    if not tenant_id:
        return None
    try:
        from app.models.tenant import Tenant
        tenant = db.get(Tenant, tenant_id)
        return tenant.plan if tenant else None
    except Exception:
        return None


def resolve_network_take_rate(plan: str | None) -> float:
    """Take-rate de REDE por plano (Rede Aumigão fornece o passeador).

    Pro → 18% / Enterprise → 10%. Aplica o mapeamento legado:
    starter/business → Pro (18%); enterprise → Enterprise (10%).

    Independe de PRICING_V2_ENABLED — a taxa de rede é sempre calculada pela
    tabela v2 porque o conceito de Rede só existe em v2.
    """
    return network_commission_default_for_plan(plan)


_NETWORK_ACCESS_TYPES = {"shared_network", "tenant_exclusive"}


def is_network_walk(db: Session, tenant_id: str | None, walker_id: str | None) -> bool:
    """Retorna True se o walker atende o tenant via Rede Aumigão.

    Sinal real: TenantWalkerAccess com access_type ∈ {shared_network, tenant_exclusive}
    e status=active para o par (tenant_id, walker_id).

    Retorna False quando qualquer argumento for None (sem sinal = passeio próprio).
    """
    if not tenant_id or not walker_id:
        return False
    try:
        from app.models.tenant_walker_access import TenantWalkerAccess
        twa = (
            db.query(TenantWalkerAccess)
            .filter(
                TenantWalkerAccess.tenant_id == tenant_id,
                TenantWalkerAccess.walker_user_id == walker_id,
                TenantWalkerAccess.status == "active",
                TenantWalkerAccess.access_type.in_(list(_NETWORK_ACCESS_TYPES)),
            )
            .first()
        )
        return twa is not None
    except Exception:
        return False


def get_commission_percent_for_walk(
    db: Session,
    tenant_id: str | None,
    *,
    walker_id: str | None = None,
) -> float:
    """Comissão completa para um passeio — escolhe entre taxa própria e de REDE.

    Lógica (v2):
      1. Override por par (TenantWalkerAccess.commission_percent) — sempre tem prioridade.
      2. Se is_network_walk(tenant_id, walker_id) → resolve_network_take_rate(plan).
      3. Caso contrário → get_commission_percent() (taxa própria, inclui config do tenant).

    Quando walker_id for None, cai direto em get_commission_percent() (zero-regressão).
    """
    # Nível 1: override por par (idêntico ao get_commission_percent nível 1).
    if tenant_id and walker_id:
        try:
            from app.models.tenant_walker_access import TenantWalkerAccess
            twa = (
                db.query(TenantWalkerAccess)
                .filter(
                    TenantWalkerAccess.tenant_id == tenant_id,
                    TenantWalkerAccess.walker_user_id == walker_id,
                    TenantWalkerAccess.status == "active",
                )
                .first()
            )
            if twa is not None and twa.commission_percent is not None:
                return float(twa.commission_percent)
        except Exception:
            pass

    # Nível 2: se passeio de rede → taxa de rede do plano.
    if is_network_walk(db, tenant_id, walker_id):
        plan = _tenant_plan(db, tenant_id)
        return resolve_network_take_rate(plan)

    # Nível 3: taxa própria (config do tenant ou default do plano).
    return get_commission_percent(db, tenant_id, walker_id=walker_id)


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


def build_payment_split(
    db: Session,
    tenant_id: str | None,
    amount: float,
    *,
    walker_id: str | None = None,
) -> dict[str, float]:
    """Monta o split para um pagamento.

    Quando walker_id for fornecido, usa get_commission_percent com precedência
    por par (TenantWalkerAccess.commission_percent → config → plano).
    Quando walker_id for None, comportamento idêntico ao original.
    """
    return compute_split(
        amount,
        get_commission_percent(db, tenant_id, walker_id=walker_id),
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
