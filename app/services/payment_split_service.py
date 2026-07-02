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

  Flag PRICING_V2_ENABLED (default False):
    OFF → get_commission_percent_for_walk ≡ get_commission_percent (zero-regressão).
          Ramo de rede NÃO é ativado — walks de rede usam a taxa própria legada.
    ON  → ramo de rede ativo: is_network_walk → resolve_network_take_rate (18/10).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.money import q2, q4, to_float, to_money

from app.models.tenant_payment_config import (
    DEFAULT_COMMISSION_PERCENT,
    TenantPaymentConfig,
    _PRICING_V2_ENABLED,
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

    Com PRICING_V2_ENABLED=False (default): comportamento IDÊNTICO a
    get_commission_percent — o ramo de rede NÃO é ativado. Zero-regressão
    garantida: walks de rede usam a taxa própria/config legada do tenant.

    Com PRICING_V2_ENABLED=True:
      1. Override por par (TenantWalkerAccess.commission_percent) — sempre tem prioridade.
      2. Se is_network_walk(tenant_id, walker_id) → resolve_network_take_rate(plan).
      3. Caso contrário → get_commission_percent() (taxa própria, inclui config do tenant).

    Quando walker_id for None, cai direto em get_commission_percent() em ambos os modos.
    """
    # Flag OFF → comportamento legado completo (equivalente a get_commission_percent).
    if not _PRICING_V2_ENABLED:
        return get_commission_percent(db, tenant_id, walker_id=walker_id)

    # Flag ON → lógica v2 com ramo de rede.

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

    Aritmética em Decimal (sem drift de float); retorna float na borda (contrato).
    """
    price = q2(walk_price)
    pct = to_money(plan_discount_percent or 0)
    if pct < Decimal("0"):
        pct = Decimal("0")
    elif pct > Decimal("100"):
        pct = Decimal("100")
    plan_discount = q2(price * pct / Decimal("100"))
    total = q2(price - plan_discount)
    return {
        "walk_price": to_float(price),
        "plan_discount_percent": to_float(pct),
        "plan_discount": to_float(plan_discount),
        "total": to_float(total),
    }


def build_quote(db: Session, tenant_id: str | None, walk_price: float) -> dict[str, float]:
    return compute_quote(walk_price, get_plan_discount_percent(db, tenant_id))


def compute_split(amount: float, commission_percent: float, tenant_margin_percent: float = 0.0) -> dict[str, float]:
    """Divide um valor entre plataforma, tenant e passeador.

    Aritmética 100% em Decimal (sem misturar float) com ROUND_HALF_UP em centavos.
    INVARIANTE: platform + tenant + walker == amount, EXATAMENTE (sem resíduo de
    centavo). walker_amount é o RESÍDUO (amount − platform − tenant), então a soma
    reconcilia por construção. Retorna float na borda para preservar o contrato.
    """
    amount_d = q2(amount)
    commission_d = to_money(commission_percent)
    if commission_d < Decimal("0"):
        commission_d = Decimal("0")
    elif commission_d > Decimal("100"):
        commission_d = Decimal("100")
    margin_d = to_money(tenant_margin_percent)
    if margin_d < Decimal("0"):
        margin_d = Decimal("0")
    # Validacao: plataforma + margem nao podem ultrapassar 90%
    if commission_d + margin_d > Decimal("90"):
        margin_d = commission_d.__class__("90") - commission_d
        if margin_d < Decimal("0"):
            margin_d = Decimal("0")
    platform_amount = q2(amount_d * commission_d / Decimal("100"))
    tenant_amount = q2(amount_d * margin_d / Decimal("100"))
    walker_amount = q2(amount_d - platform_amount - tenant_amount)
    return {
        "commission_percent": to_float(commission_d),
        "tenant_margin_percent": to_float(margin_d),
        "platform_amount": to_float(platform_amount),
        "tenant_amount": to_float(tenant_amount),
        "walker_amount": to_float(walker_amount),
    }


def walker_percent_from_split(split: dict[str, float]) -> float:
    """Percentual repassado ao walker no gateway, derivado dos amounts do split.

    Fonte única (R2/R10): walker_amount / (platform+tenant+walker). Honra a margem
    do tenant e mantém o repasse no gateway igual ao repasse contábil de compute_split.
    Retorna 0.0 quando o total é zero (sem divisão por zero).
    """
    total = (
        to_money(split["platform_amount"])
        + to_money(split.get("tenant_amount", 0.0))
        + to_money(split["walker_amount"])
    )
    if total <= Decimal("0"):
        return 0.0
    pct = q4(to_money(split["walker_amount"]) / total * Decimal("100"))
    return to_float(pct)


def build_payment_split(
    db: Session,
    tenant_id: str | None,
    amount: float,
    *,
    walker_id: str | None = None,
) -> dict[str, float]:
    """Monta o split para um pagamento.

    Delega a resolução de comissão a get_commission_percent_for_walk, que:
    - Com PRICING_V2_ENABLED=False (default): equivale a get_commission_percent —
      zero-regressão em todos os call sites (mesmo comportamento de hoje).
    - Com PRICING_V2_ENABLED=True: ativa o ramo de rede (18/10% quando o walker
      atende via Rede Aumigão).

    Quando walker_id for None, o comportamento é IDÊNTICO ao original em ambos
    os modos (sem walker → sem sinal de rede → mesma comissão de antes).
    """
    return compute_split(
        amount,
        get_commission_percent_for_walk(db, tenant_id, walker_id=walker_id),
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
