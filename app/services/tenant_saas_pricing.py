"""Preços canônicos da mensalidade SaaS do tenant (Projeto B)."""
import logging

logger = logging.getLogger("aumigao.tenant_saas_pricing")

TENANT_SAAS_PRICE: dict[str, float] = {"pro": 129.90, "enterprise": 1199.90}


def resolve_saas_price(plan: str, custom_price: float | None) -> float:
    """Pro fixo; Enterprise usa custom_price (>0) ou o piso. custom_price=0/negativo é erro.
    Plano fora do catálogo SaaS cai no piso do Pro com warning (legado starter/business)."""
    if plan not in TENANT_SAAS_PRICE:
        logger.warning("resolve_saas_price: plano '%s' fora do catálogo SaaS; usando piso Pro", plan)
    base = TENANT_SAAS_PRICE.get(plan, TENANT_SAAS_PRICE["pro"])
    if plan == "enterprise" and custom_price is not None:
        if float(custom_price) <= 0:
            raise ValueError(f"custom_price deve ser positivo; recebido: {custom_price}")
        return float(custom_price)
    return base
