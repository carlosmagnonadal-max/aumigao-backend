"""Seed do catálogo base de planos recorrentes por tenant.

Localizado em app/services/ (mesmo pacote de recurring_plan_service) para reusar
os imports existentes sem criar dependências circulares. Um módulo separado
(recurring_plan_seed.py) é preferível a adicionar ao recurring_plan_service.py
porque:
  1. Mantém o arquivo de serviço principal abaixo de 500 linhas.
  2. Deixa claro que seed é responsabilidade de ciclo de vida (onboarding),
     não de negócio recorrente (subscrição/créditos).
  3. Facilita testes isolados sem carregar dependências de httpx/Asaas.

Convenção de commit:
    seed_base_recurring_plans faz db.add + db.flush (não commit).
    O CALLER (rota create_tenant) é responsável pelo commit da transação
    completa. Isso garante atomicidade: se qualquer etapa do create_tenant
    falhar após o seed (ex.: audit_log), o banco faz rollback de tudo.
"""
import logging

from sqlalchemy.orm import Session

from app.models.recurring_plan import RECURRING_PLANS_FEATURE_KEY, RecurringPlan
from app.models.tenant import Tenant
from app.services.tenant_plan_service import plan_allows_product_feature

logger = logging.getLogger("aumigao.recurring_plan_seed")

# ── Catálogo canônico ──────────────────────────────────────────────────────────
# Fonte da verdade para os 9 planos base que todo tenant elegível deve receber.
# Cada dict contém apenas os campos de domínio; tenant_id e active são injetados
# em seed_base_recurring_plans.
# Re-preço 07/07/2026 (decisão de unit economics): preço por passeio NUNCA
# abaixo do repasse do passeador na âncora cheia (R$44,91 na âncora de 54,90).
# Desconto de 15-18% vs âncora, co-financiado pelas fatias de plataforma e
# tenant — nunca pelo passeador. Ver plan_walk_economics.py e
# memory/planos-repasse-decisao-2026-07-07.md.
BASE_RECURRING_PLANS: list[dict] = [
    # Mensais (R$46,00-46,24/passeio · ~16% off da âncora 54,90)
    {"name": "Leve Mensal",       "description": "8 passeios por mes",                  "price":   369.90, "walks_per_cycle":   8, "interval": "monthly"},
    {"name": "Ativo Mensal",      "description": "12 passeios por mes",                 "price":   554.90, "walks_per_cycle":  12, "interval": "monthly"},
    {"name": "Intenso Mensal",    "description": "20 passeios por mes",                 "price":   919.90, "walks_per_cycle":  20, "interval": "monthly"},
    # Semestrais (R$45,08-45,21/passeio · ~17,7% off)
    {"name": "Leve Semestral",    "description": "48 passeios no semestre (8/mes)",     "price":  2169.90, "walks_per_cycle":  48, "interval": "semiannual"},
    {"name": "Ativo Semestral",   "description": "72 passeios no semestre (12/mes)",    "price":  3254.90, "walks_per_cycle":  72, "interval": "semiannual"},
    {"name": "Intenso Semestral", "description": "120 passeios no semestre (20/mes)",   "price":  5409.90, "walks_per_cycle": 120, "interval": "semiannual"},
    # Anuais (R$45,07-45,10/passeio · ~17,9% off)
    {"name": "Leve Anual",        "description": "96 passeios no ano (8/mes)",          "price":  4329.90, "walks_per_cycle":  96, "interval": "yearly"},
    {"name": "Ativo Anual",       "description": "144 passeios no ano (12/mes)",        "price":  6489.90, "walks_per_cycle": 144, "interval": "yearly"},
    {"name": "Intenso Anual",     "description": "240 passeios no ano (20/mes)",        "price": 10819.90, "walks_per_cycle": 240, "interval": "yearly"},
]


def seed_base_recurring_plans(db: Session, tenant: Tenant) -> int:
    """Semeia o catálogo base de planos recorrentes para um tenant novo.

    Comportamento:
    - IDEMPOTENTE: se o tenant já tem qualquer linha em recurring_plans,
      não faz nada e retorna 0. Cobre re-deploys e chamadas duplicadas.
    - Gate de plano: só semeia se plan_allows_product_feature(tenant,
      RECURRING_PLANS_FEATURE_KEY) retornar True (business/enterprise no v1;
      pro/enterprise no v2). Tenants em planos inelegíveis retornam 0.
    - Cria os 9 RecurringPlan com tenant_id do tenant e active=True.
    - NÃO faz commit — usa db.add + db.flush para que o caller possa
      controlar a transação (atomicidade com create_tenant).

    Retorna:
        int — quantidade de planos criados (9 em novo tenant elegível, 0 caso contrário).
    """
    # Gate 1: plano comercial deve permitir o módulo
    if not plan_allows_product_feature(tenant, RECURRING_PLANS_FEATURE_KEY):
        logger.debug(
            "seed_base_recurring_plans: plano '%s' não elegível para recurring_plans — pulando tenant=%s",
            tenant.plan,
            tenant.id,
        )
        return 0

    # Gate 2: idempotência — verifica existência de qualquer plano para este tenant
    existing_count = (
        db.query(RecurringPlan)
        .filter(RecurringPlan.tenant_id == tenant.id)
        .count()
    )
    if existing_count > 0:
        logger.debug(
            "seed_base_recurring_plans: tenant=%s já tem %d planos — idempotente, pulando",
            tenant.id,
            existing_count,
        )
        return 0

    # Semeia os 9 planos base
    for spec in BASE_RECURRING_PLANS:
        plan = RecurringPlan(
            tenant_id=tenant.id,
            name=spec["name"],
            description=spec["description"],
            price=spec["price"],
            walks_per_cycle=spec["walks_per_cycle"],
            interval=spec["interval"],
            active=True,
        )
        db.add(plan)

    db.flush()  # Persiste sem commit — o caller commita junto com o resto da transação
    logger.info(
        "seed_base_recurring_plans: %d planos base criados para tenant=%s (plan=%s)",
        len(BASE_RECURRING_PLANS),
        tenant.id,
        tenant.plan,
    )
    return len(BASE_RECURRING_PLANS)
