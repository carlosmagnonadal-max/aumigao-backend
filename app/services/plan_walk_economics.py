"""Economia do passeio coberto por plano recorrente (decisão de 07/07/2026).

Princípios (regra de ouro fechada com o Carlos):
1. PASSEADOR INTOCADO EM REAIS: no passeio de plano ele recebe exatamente o
   mesmo residual que receberia no passeio avulso à âncora cheia.
2. CO-FINANCIAMENTO PRO-RATA: o desconto do plano é bancado por plataforma e
   tenant na proporção das suas fatias (comissão : margem). O que sobra do
   valor efetivo do plano depois de pagar o passeador é dividido pro-rata.
3. PISO DINÂMICO: desconto máximo do plano = min(comissão + margem do tenant,
   take de rede). Abaixo do piso é impossível qualquer elo ficar negativo, em
   qualquer mix próprio/rede e com 100% de uso dos créditos. A quebra
   (créditos expiram no ciclo) é upside puro.

O admin-web expõe tudo isso como painel editável (regra canônica): o tenant
escolhe o desconto que quiser ATÉ o piso, vendo o lucro de cada elo em reais.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.core.money import q2, to_float, to_money
from app.services.payment_split_service import (
    get_commission_percent_for_walk,
    get_tenant_margin_percent,
)


def compute_plan_walk_split(
    effective_amount: float,
    anchor_amount: float,
    commission_percent: float,
    tenant_margin_percent: float = 0.0,
) -> dict[str, float]:
    """Split do passeio de PLANO: passeador pela âncora, resto pro-rata.

    - walker_amount = residual da ÂNCORA cheia (idêntico ao avulso — princípio 1).
    - O que sobra do valor EFETIVO do plano (effective − walker) é dividido
      entre plataforma e tenant na proporção comissão:margem (princípio 2).
    - Se o plano for deficitário (legado, criado antes do piso), o walker segue
      intocado e o déficit aparece como tenant_amount NEGATIVO — contabilidade
      honesta; o piso impede planos novos de chegarem aqui.

    INVARIANTE: platform + tenant + walker == effective_amount, exatamente.
    """
    effective_d = q2(effective_amount)
    anchor_d = q2(anchor_amount)
    commission_d = to_money(max(0.0, min(100.0, commission_percent)))
    margin_d = to_money(max(0.0, tenant_margin_percent))

    walker_d = q2(anchor_d * (Decimal("100") - commission_d - margin_d) / Decimal("100"))
    if walker_d < Decimal("0"):
        walker_d = Decimal("0")

    remaining = q2(effective_d - walker_d)
    slices = commission_d + margin_d
    if remaining >= Decimal("0") and slices > Decimal("0"):
        platform_d = q2(remaining * commission_d / slices)
    elif remaining >= Decimal("0"):
        # Sem fatias configuradas (c=m=0): sobra vira da plataforma por default.
        platform_d = remaining
    else:
        # Déficit (plano legado abaixo do piso): plataforma não fica negativa;
        # o tenant — que precificou o plano — absorve o buraco (visível).
        platform_d = Decimal("0")
    tenant_d = q2(remaining - platform_d)

    return {
        "commission_percent": to_float(commission_d),
        "tenant_margin_percent": to_float(margin_d),
        "platform_amount": to_float(platform_d),
        "tenant_amount": to_float(tenant_d),
        "walker_amount": to_float(walker_d),
    }


def max_plan_discount_percent(
    commission_percent: float,
    tenant_margin_percent: float,
    network_take_percent: float,
) -> float:
    """Piso dinâmico (princípio 3): min(comissão + margem, take de rede).

    - Ramo próprio: o desconto é co-financiado pelas fatias de plataforma e
      tenant → cabe até (comissão + margem).
    - Ramo rede: o desconto sai do take de rede da plataforma → cabe até ele.
    Como o mix é imprevisível (plano vendido antecipado), vale o MENOR dos dois:
    qualquer mix futuro fica não-negativo pra todo elo.
    """
    own_cap = max(0.0, float(commission_percent)) + max(0.0, float(tenant_margin_percent))
    network_cap = max(0.0, float(network_take_percent))
    return to_float(q2(min(own_cap, network_cap)))


def plan_pricing_floor(db: Session, tenant_id: str) -> dict:
    """Números do piso e das fatias pro tenant — alimenta o painel do admin
    (regra canônica: tudo editável ATÉ o piso, com o lucro de cada elo visível)
    e a validação server-side do create/update de plano.
    """
    from app.models.tenant import Tenant
    from app.services import individual_walk_pricing_service as pricing_svc
    from app.services.payment_split_service import (
        get_commission_percent,
        resolve_network_take_rate,
    )

    pricing = pricing_svc.get_or_create_config(db, tenant_id)
    anchor_45 = float(pricing.price_45 or 0.0)
    commission = float(get_commission_percent(db, tenant_id))
    margin = float(get_tenant_margin_percent(db, tenant_id))
    tenant = db.get(Tenant, tenant_id)
    network_take = float(resolve_network_take_rate(getattr(tenant, "plan", None)))
    cap = max_plan_discount_percent(commission, margin, network_take)
    walker_45 = to_float(q2(to_money(anchor_45) * (Decimal("100") - to_money(commission) - to_money(margin)) / Decimal("100")))
    min_per_walk = to_float(q2(to_money(anchor_45) * (Decimal("100") - to_money(cap)) / Decimal("100")))
    return {
        "anchor_price_45": anchor_45,
        "commission_percent": commission,
        "tenant_margin_percent": margin,
        "network_take_percent": network_take,
        "walker_amount_45": walker_45,
        "max_discount_percent": cap,
        "min_per_walk_price": min_per_walk,
    }


def enforce_plan_pricing_floor(db: Session, tenant_id: str, price: float, walks_per_cycle: int) -> None:
    """Trava do piso (princípio 3): recusa plano cujo preço por passeio fique
    abaixo do mínimo sustentável do tenant. 400 com a conta na mensagem.

    Planos sem passeios ou sem preço (placeholders) passam — não há economia
    a validar.
    """
    from fastapi import HTTPException

    if not (walks_per_cycle and walks_per_cycle > 0 and price and price > 0):
        return
    floor = plan_pricing_floor(db, tenant_id)
    if floor["anchor_price_45"] <= 0:
        return
    per_walk = price / walks_per_cycle
    if per_walk + 0.005 < floor["min_per_walk_price"]:
        discount = (1 - per_walk / floor["anchor_price_45"]) * 100
        raise HTTPException(
            status_code=400,
            detail=(
                f"Preço por passeio de R${per_walk:.2f} fica abaixo do mínimo sustentável de "
                f"R${floor['min_per_walk_price']:.2f} (desconto de {discount:.1f}% excede o teto de "
                f"{floor['max_discount_percent']:.1f}% = menor fatia entre comissão+margem e take de rede). "
                f"Aumente o preço do plano, reduza os passeios por ciclo ou ajuste sua margem."
            ),
        )


def resolve_plan_walk_split(
    db: Session,
    walk,
    subscription,
    *,
    walker_id: str | None,
) -> dict[str, float]:
    """Resolve o split de um passeio coberto por assinatura.

    - effective = preço do ciclo ÷ passeios do ciclo (snapshot da assinatura —
      imune a re-preço posterior do plano).
    - anchor = walk.price (preço avulso cheio registrado no walk).
    - comissão resolvida por walker (própria vs rede) — a mesma função do avulso.
    """
    walks_per_cycle = int(getattr(subscription, "walks_per_cycle", 0) or 0)
    sub_price = float(getattr(subscription, "price", 0.0) or 0.0)
    anchor = float(getattr(walk, "price", 0.0) or 0.0)
    effective = (sub_price / walks_per_cycle) if walks_per_cycle > 0 and sub_price > 0 else anchor
    return compute_plan_walk_split(
        effective,
        anchor,
        get_commission_percent_for_walk(db, getattr(walk, "tenant_id", None), walker_id=walker_id),
        get_tenant_margin_percent(db, getattr(walk, "tenant_id", None)),
    )
