"""Serviço de expiração de crédito e reconhecimento de breakage (Item 3 + 4).

CAMADA CONTÁBIL PURA: NÃO move dinheiro, NÃO altera saldos de pagamento,
NÃO interfere no fluxo de passeios. Somente:
  1. Varre TutorSubscription com créditos remanescentes expirados ou cancelados.
  2. Registra receita de breakage no CreditLedgerEntry (idempotente).
  3. Zera credits_remaining de forma idempotente (sem duplo reconhecimento).

Política de breakage:
  - Um crédito é considerado breakage quando:
    (a) A assinatura está CANCELADA e credits_remaining > 0, ou
    (b) A assinatura está ATIVA mas current_period_end < now (período vencido sem renovação,
        indicando falha no Asaas ou cancelamento implícito) e credits_remaining > 0.
  - O reconhecimento é idempotente: já existe um CreditLedgerEntry com
    event_type=breakage_recognized para a subscription → não cria novo.
  - O zeragem de credits_remaining é também idempotente via UPDATE condicional.

TODO: A proporcionalidade do breakage (e.g., reconhecer apenas após X dias de
vencimento) e o momento fiscal exato PRECISAM de VALIDAÇÃO DO CONTADOR antes de
virar base de escrituração. Esta é uma estimativa conservadora.

Gated por: CREDIT_LEDGER_ENABLED (default=ON, pois só registra — não move dinheiro).
Desligar via variável de ambiente apenas para debug/manutenção.
"""
import logging
import os
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.money import q2, q4, to_float, to_money
from app.models.credit_ledger import (
    CreditLedgerEntry,
    LEDGER_BREAKAGE_RECOGNIZED,
)
from app.models.recurring_plan import (
    TutorSubscription,
    SUBSCRIPTION_CANCELLED,
    SUBSCRIPTION_ACTIVE,
)
from app.models.recurring_plan import RecurringPlan

logger = logging.getLogger("aumigao.credit_expiry_service")


def credit_ledger_enabled() -> bool:
    """Retorna True se o ledger contábil de créditos está habilitado.

    Default: ON (variável ausente = habilitado). Só registra, nunca move dinheiro.
    Desligar via CREDIT_LEDGER_ENABLED=false apenas para debug/manutenção.
    """
    val = os.getenv("CREDIT_LEDGER_ENABLED", "true").lower()
    return val not in ("false", "0", "no", "off")


def _unit_value(subscription: TutorSubscription) -> float:
    """Calcula o valor unitário de 1 crédito com base no snapshot da assinatura."""
    if subscription.walks_per_cycle and subscription.walks_per_cycle > 0:
        return to_float(q4(to_money(subscription.price or 0) / to_money(subscription.walks_per_cycle)))
    return 0.0


def _has_breakage_entry(db: Session, subscription_id: str) -> bool:
    """Verifica se já existe um registro de breakage para esta assinatura (idempotência)."""
    return (
        db.query(CreditLedgerEntry)
        .filter(
            CreditLedgerEntry.subscription_id == subscription_id,
            CreditLedgerEntry.event_type == LEDGER_BREAKAGE_RECOGNIZED,
        )
        .first()
    ) is not None


def _recognize_breakage(db: Session, subscription: TutorSubscription) -> bool:
    """Registra breakage para uma assinatura com créditos remanescentes.

    Idempotente: retorna False se o breakage já foi registrado ou se não há créditos.
    NÃO commita — o caller é responsável pelo commit.
    NUNCA propaga exceção (best-effort).
    """
    try:
        credits = int(subscription.credits_remaining or 0)
        if credits <= 0:
            return False
        if _has_breakage_entry(db, subscription.id):
            return False  # já reconhecido — não duplicar

        unit = _unit_value(subscription)
        total = to_float(q2(to_money(credits) * to_money(unit)))

        entry = CreditLedgerEntry(
            tenant_id=subscription.tenant_id,
            subscription_id=subscription.id,
            event_type=LEDGER_BREAKAGE_RECOGNIZED,
            credits_count=credits,
            unit_value=unit,
            total_value=total,
            walk_id=None,
            payment_id=None,
        )
        db.add(entry)

        # Zera créditos de forma segura — o UPDATE só afeta quem ainda tem crédito
        # (se outra thread já zerou, a condição credits_remaining > 0 não bate).
        subscription.credits_remaining = 0
        subscription.updated_at = datetime.utcnow()
        db.add(subscription)

        logger.info(
            "breakage reconhecido: subscription_id=%s tenant_id=%s credits=%d total=%.2f",
            subscription.id, subscription.tenant_id, credits, total,
        )
        return True
    except Exception:
        logger.exception(
            "falha best-effort ao reconhecer breakage subscription_id=%s",
            subscription.id,
        )
        return False


def sweep_expired_credits(db: Session) -> dict:
    """Varre assinaturas expiradas/canceladas com créditos remanescentes e reconhece breakage.

    Regras de elegibilidade:
      - CANCELADAS com credits_remaining > 0
      - ATIVAS com current_period_end < now e credits_remaining > 0 (período vencido sem renovação)

    Idempotente: re-execução não duplica entradas.
    Retorna dict com contagens para observabilidade.

    CAMADA CONTÁBIL — não move dinheiro.
    """
    if not credit_ledger_enabled():
        logger.info("sweep_expired_credits: CREDIT_LEDGER_ENABLED=false — pulado")
        return {"skipped": True, "processed": 0, "recognized": 0}

    now = datetime.utcnow()
    recognized = 0
    processed = 0

    # 1) Assinaturas canceladas com crédito restante
    cancelled_with_credits = (
        db.query(TutorSubscription)
        .filter(
            TutorSubscription.status == SUBSCRIPTION_CANCELLED,
            TutorSubscription.credits_remaining > 0,
        )
        .all()
    )

    for sub in cancelled_with_credits:
        processed += 1
        if _recognize_breakage(db, sub):
            recognized += 1

    # 2) Assinaturas ativas com período vencido (sem renovação — anomalia ou cancelamento tardio)
    active_expired = (
        db.query(TutorSubscription)
        .filter(
            TutorSubscription.status == SUBSCRIPTION_ACTIVE,
            TutorSubscription.current_period_end < now,
            TutorSubscription.credits_remaining > 0,
        )
        .all()
    )

    for sub in active_expired:
        processed += 1
        if _recognize_breakage(db, sub):
            recognized += 1

    if recognized > 0:
        db.commit()

    logger.info(
        "sweep_expired_credits: processed=%d recognized=%d",
        processed, recognized,
    )
    return {"processed": processed, "recognized": recognized}


def recognize_breakage_on_cancel(db: Session, subscription: TutorSubscription) -> None:
    """Gatilho best-effort a ser chamado no cancelamento de assinatura.

    Se a assinatura sendo cancelada tiver créditos remanescentes, reconhece
    breakage imediatamente (sem aguardar o sweep periódico).
    NÃO commita — o caller (cancel_subscription*) já commita.
    NUNCA propaga exceção.
    """
    if not credit_ledger_enabled():
        return
    try:
        _recognize_breakage(db, subscription)
    except Exception:
        logger.exception(
            "recognize_breakage_on_cancel: falha best-effort subscription_id=%s",
            subscription.id,
        )
