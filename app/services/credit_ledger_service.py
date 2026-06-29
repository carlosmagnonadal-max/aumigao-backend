"""Operações do ledger contábil de crédito (Item 4 — CPC 47).

CAMADA CONTÁBIL PURA: NÃO move dinheiro, NÃO altera saldos.
Todas as funções são best-effort: NUNCA propagam exceção ao caller.

Gated por CREDIT_LEDGER_ENABLED (default=ON).

Idempotência:
  - liability_created: 1 por (subscription_id, cycle_reference) — P1: cada
    renovação mensal registra um novo passivo de contrato (CPC 47 §106).
  - revenue_recognized: 1 por (subscription_id, walk_id).
  - breakage_recognized: 1 por subscription_id.

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
    """Retorna o valor unitário do crédito: subscription.price / walks_per_cycle.

    P3 (CPC 47 §106): base SEMPRE bruta — preço cheio do plano conforme snapshot
    no momento da assinatura. Sem dedução de comissão de plataforma ou taxa de
    passeador. Comissão é custo operacional reconhecido separadamente.
    """
    if subscription.walks_per_cycle and subscription.walks_per_cycle > 0:
        return round(float(subscription.price or 0.0) / subscription.walks_per_cycle, 4)
    return 0.0


def _cycle_reference(subscription: TutorSubscription) -> str | None:
    """Retorna a chave de ciclo: current_period_start formatado como 'YYYY-MM-DD'.

    Ciclos mensais nunca começam 2× no mesmo dia para a mesma assinatura,
    portanto esta chave é suficiente para idempotência por ciclo (P1).
    Compatível com backfill SQL via to_char(current_period_start, 'YYYY-MM-DD').
    Retorna None se current_period_start não estiver definido.
    """
    cps = getattr(subscription, "current_period_start", None)
    return cps.date().isoformat() if cps else None


def _entry_exists(
    db: Session,
    subscription_id: str,
    event_type: str,
    walk_id: str | None = None,
    cycle_reference: str | None = None,
) -> bool:
    """Verifica se o evento já foi registrado (idempotência).

    Para liability_created: filtra também por cycle_reference quando fornecido
    (1 liability por ciclo, não por subscription inteira).
    Para revenue_recognized: filtra por walk_id.
    """
    q = db.query(CreditLedgerEntry).filter(
        CreditLedgerEntry.subscription_id == subscription_id,
        CreditLedgerEntry.event_type == event_type,
    )
    if walk_id is not None:
        q = q.filter(CreditLedgerEntry.walk_id == walk_id)
    if cycle_reference is not None:
        q = q.filter(CreditLedgerEntry.cycle_reference == cycle_reference)
    return q.first() is not None


def record_liability_safe(db: Session, subscription: TutorSubscription, payment_id: str | None = None) -> None:
    """Registra o passivo de crédito (receita diferida) quando os créditos são concedidos.

    Chamado em:
      - subscribe() — 1ª concessão síncrona.
      - grant_credits_on_payment() — 1ª concessão via webhook (ciclo 1).
      - reset_credits_if_renewal() — renovações de ciclos subsequentes (P1).

    Idempotente por (subscription_id, cycle_reference): cada ciclo mensal pode
    registrar exatamente 1 passivo. current_period_start já deve estar atualizado
    pelo caller antes de invocar esta função (reset_credits_if_renewal o faz).

    Savepoint: o insert é envolvido em begin_nested() para que uma violação de
    constraint em corrida seja absorvida sem envenenar a transação externa.

    Best-effort: falha no ledger NUNCA quebra a concessão de créditos.
    NÃO commita.
    """
    if not credit_ledger_enabled():
        return
    try:
        cycle_ref = _cycle_reference(subscription)
        if _entry_exists(db, subscription.id, LEDGER_LIABILITY_CREATED, cycle_reference=cycle_ref):
            return  # já registrado para este ciclo
        credits = int(subscription.walks_per_cycle or 0)
        unit = _unit_value(subscription)
        total = round(credits * unit, 2)
        entry = CreditLedgerEntry(
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            event_type=LEDGER_LIABILITY_CREATED,
            cycle_reference=cycle_ref,
            credits_count=credits,
            unit_value=unit,
            total_value=total,
            walk_id=None,
            payment_id=payment_id,
        )
        # Savepoint: contém violação de índice único em corrida sem envenenar
        # a transação externa. Honra o invariante best-effort.
        with db.begin_nested():
            db.add(entry)
            db.flush()
        logger.debug(
            "liability_created: subscription_id=%s cycle=%s credits=%d total=%.2f",
            subscription.id, cycle_ref, credits, total,
        )
    except Exception:
        logger.exception(
            "record_liability_safe: falha best-effort subscription_id=%s", subscription.id
        )


def record_revenue_recognized_safe(db: Session, subscription: TutorSubscription, walk_id: str) -> None:
    """Registra reconhecimento de receita quando 1 crédito é consumido num passeio.

    Chamado após consume_credit_if_available bem-sucedido (no caminho de criação do passeio).
    Idempotente por (subscription_id, revenue_recognized, walk_id).
    cycle_reference é preenchido de forma informativa (qual ciclo originou a receita);
    a idempotência de revenue NÃO inclui cycle_reference no filtro.
    Best-effort: falha NUNCA quebra o consumo de crédito.
    NÃO commita.
    """
    if not credit_ledger_enabled():
        return
    try:
        if _entry_exists(db, subscription.id, LEDGER_REVENUE_RECOGNIZED, walk_id=walk_id):
            return  # já registrado
        unit = _unit_value(subscription)
        entry = CreditLedgerEntry(
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            event_type=LEDGER_REVENUE_RECOGNIZED,
            cycle_reference=_cycle_reference(subscription),  # informativo
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
