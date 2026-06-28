"""Operações do ledger contábil de crédito (Item 4 — CPC 47).

CAMADA CONTÁBIL PURA: NÃO move dinheiro, NÃO altera saldos.
Todas as funções são best-effort: NUNCA propagam exceção ao caller.

Gated por CREDIT_LEDGER_ENABLED (default=ON).

TODO: O momento exato de reconhecimento de PIS/COFINS, a proporcionalidade do
breakage e a base de cálculo do passivo PRECISAM DE VALIDAÇÃO DO CONTADOR antes
de virar base de escrituração. Esta é uma ESTIMATIVA.
"""
import logging

from sqlalchemy.orm import Session

from app.models.credit_ledger import (
    CreditLedgerEntry,
    LEDGER_LIABILITY_CREATED,
    LEDGER_REVENUE_RECOGNIZED,
    LEDGER_BREAKAGE_RECOGNIZED,
)
from app.models.recurring_plan import TutorSubscription
from app.services.credit_expiry_service import credit_ledger_enabled

logger = logging.getLogger("aumigao.credit_ledger_service")


def _unit_value(subscription: TutorSubscription) -> float:
    if subscription.walks_per_cycle and subscription.walks_per_cycle > 0:
        return round(float(subscription.price or 0.0) / subscription.walks_per_cycle, 4)
    return 0.0


def _entry_exists(db: Session, subscription_id: str, event_type: str, walk_id: str | None = None) -> bool:
    """Verifica se o evento já foi registrado (idempotência)."""
    q = db.query(CreditLedgerEntry).filter(
        CreditLedgerEntry.subscription_id == subscription_id,
        CreditLedgerEntry.event_type == event_type,
    )
    if walk_id is not None:
        q = q.filter(CreditLedgerEntry.walk_id == walk_id)
    return q.first() is not None


def record_liability_safe(db: Session, subscription: TutorSubscription, payment_id: str | None = None) -> None:
    """Registra o passivo de crédito (receita diferida) quando os créditos são concedidos.

    Chamado em grant_credits_on_payment / subscribe (1ª concessão de créditos).
    Idempotente: verifica existência antes de inserir (1 liability por subscription).
    Best-effort: falha no ledger NUNCA quebra a concessão de créditos.
    NÃO commita.
    """
    if not credit_ledger_enabled():
        return
    try:
        if _entry_exists(db, subscription.id, LEDGER_LIABILITY_CREATED):
            return  # já registrado
        credits = int(subscription.walks_per_cycle or 0)
        unit = _unit_value(subscription)
        total = round(credits * unit, 2)
        entry = CreditLedgerEntry(
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            event_type=LEDGER_LIABILITY_CREATED,
            credits_count=credits,
            unit_value=unit,
            total_value=total,
            walk_id=None,
            payment_id=payment_id,
        )
        db.add(entry)
        logger.debug(
            "liability_created: subscription_id=%s credits=%d total=%.2f",
            subscription.id, credits, total,
        )
    except Exception:
        logger.exception(
            "record_liability_safe: falha best-effort subscription_id=%s", subscription.id
        )


def record_revenue_recognized_safe(db: Session, subscription: TutorSubscription, walk_id: str) -> None:
    """Registra reconhecimento de receita quando 1 crédito é consumido num passeio.

    Chamado após consume_credit_if_available bem-sucedido (no caminho de criação do passeio).
    Idempotente por (subscription_id, revenue_recognized, walk_id).
    Best-effort: falha NUNCA quebra o consumo de crédito.
    NÃO commita.
    """
    if not credit_ledger_enabled():
        return
    try:
        if _entry_exists(db, subscription.id, LEDGER_REVENUE_RECOGNIZED, walk_id):
            return  # já registrado
        unit = _unit_value(subscription)
        entry = CreditLedgerEntry(
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            event_type=LEDGER_REVENUE_RECOGNIZED,
            credits_count=1,
            unit_value=unit,
            total_value=unit,
            walk_id=walk_id,
            payment_id=None,
        )
        db.add(entry)
        logger.debug(
            "revenue_recognized: subscription_id=%s walk_id=%s unit=%.4f",
            subscription.id, walk_id, unit,
        )
    except Exception:
        logger.exception(
            "record_revenue_recognized_safe: falha best-effort subscription_id=%s walk_id=%s",
            subscription.id, walk_id,
        )
