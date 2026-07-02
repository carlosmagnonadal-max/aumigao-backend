"""Void-de-rede automático no estorno de uma COMPRA DE CRÉDITO (P2).

Contexto (medição≠custódia): o tutor compra créditos closed-loop (CPC 47) via
assinatura; o Payment da compra NÃO carrega o walk_id de nenhum passeio. Quando
esse Payment é estornado (PAYMENT_REFUNDED / PAYMENT_REVERSED com externalReference
`sub:<id>`), este serviço reverte o que é SEGURO reverter automaticamente e emite
alerta operacional para o que é ambíguo (não faz clawback automático do passeador).

Regras (conservador):
  1. Créditos ainda NÃO consumidos: zera credits_remaining (débito atômico) e
     registra reversão do passivo no ledger (liability_reversed, total<0),
     idempotente por (subscription_id, cycle_reference).
  2. Créditos JÁ consumidos por passeios de rede (earnings acumulados): NÃO faz
     clawback automático do ganho do passeador. Apenas ALERTA os admins do tenant
     (mesmo padrão do "PIX a recuperar" em walker_payout_service) para tratativa
     manual.

Idempotência: reverter o mesmo Payment 2× não zera/reverte 2×. Não commita
(o caller — o webhook — commita na mesma transação).
"""
import logging
from datetime import datetime

from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.core.money import q2, to_float, to_money
from app.models.credit_ledger import (
    CreditLedgerEntry,
    LEDGER_LIABILITY_REVERSED,
)
from app.models.recurring_plan import TutorSubscription
from app.models.walk import Walk
from app.services.credit_expiry_service import credit_ledger_enabled

logger = logging.getLogger("aumigao.credit_refund_service")


def _unit_value(subscription: TutorSubscription) -> float:
    """Valor unitário do crédito (snapshot): price / walks_per_cycle. 0 se indefinido."""
    if subscription.walks_per_cycle and subscription.walks_per_cycle > 0:
        return to_float(
            to_money(subscription.price or 0) / to_money(subscription.walks_per_cycle)
        )
    return 0.0


def _reversal_exists(db: Session, subscription_id: str, cycle_reference: str | None) -> bool:
    """True se já existe uma reversão de passivo para este ciclo (idempotência)."""
    q = db.query(CreditLedgerEntry).filter(
        CreditLedgerEntry.subscription_id == subscription_id,
        CreditLedgerEntry.event_type == LEDGER_LIABILITY_REVERSED,
    )
    if cycle_reference is not None:
        q = q.filter(CreditLedgerEntry.cycle_reference == cycle_reference)
    return q.first() is not None


def _consumed_network_walks(db: Session, subscription_id: str) -> list[Walk]:
    """Passeios COBERTOS pela assinatura que consumiram crédito e NÃO foram estornados.

    São os passeios ambíguos: já geraram (potencialmente) WalkerEarning ao passeador.
    """
    return (
        db.query(Walk)
        .filter(
            Walk.subscription_id == subscription_id,
            Walk.credit_refunded.is_(False),
        )
        .all()
    )


def _notify_admins_credit_refund(
    db: Session,
    subscription: TutorSubscription,
    *,
    reason: str,
    consumed_count: int,
    reversed_credits: int,
) -> None:
    """Alerta os admins do tenant sobre o estorno da compra de crédito.

    Emitido quando há passeios de rede já consumidos (caso ambíguo): o passeador
    pode já ter ganho acumulado; a recuperação é manual. Best-effort — nunca
    bloqueia a reversão. Segue o padrão de _notify_admins_clawback (PIX a recuperar).
    """
    try:
        from app.models.user import User
        from app.routes.notifications import NotificationCreate, _create_notification

        admins = (
            db.query(User)
            .filter(
                User.role.in_(["admin", "super_admin"]),
                User.tenant_id == subscription.tenant_id,
            )
            .all()
            if subscription.tenant_id
            else db.query(User).filter(User.role.in_(["admin", "super_admin"])).all()
        )
        for admin in admins:
            _create_notification(db, NotificationCreate(
                user_id=admin.id,
                user_role=admin.role,
                tenant_id=subscription.tenant_id,
                title="⚠️ Estorno de compra de crédito com passeios já usados",
                message=(
                    f"O tutor teve a compra de créditos estornada ({reason}). "
                    f"{reversed_credits} crédito(s) não usado(s) foram anulados automaticamente, "
                    f"mas {consumed_count} passeio(s) de rede já foram realizados e podem ter "
                    "gerado ganho ao passeador. Verifique a recuperação manualmente."
                ),
                type="credit_refund_review",
                related_entity_type="tutor_subscription",
                related_entity_id=subscription.id,
                metadata={
                    "subscription_id": subscription.id,
                    "reason": reason,
                    "consumed_walks": consumed_count,
                    "reversed_credits": reversed_credits,
                },
            ))
    except Exception:
        logger.exception(
            "credit_refund: falha best-effort ao notificar admins subscription_id=%s",
            subscription.id,
        )


def reverse_credit_purchase(
    db: Session,
    subscription: TutorSubscription,
    *,
    reason: str,
    payment_id: str | None = None,
) -> dict:
    """Reverte automaticamente o que é seguro no estorno de uma compra de crédito.

    - Zera os créditos remanescentes de forma atômica e idempotente (débito).
    - Registra a reversão do passivo no ledger (liability_reversed, total<0),
      idempotente por (subscription_id, cycle_reference).
    - Se houver passeios de rede já consumidos (earnings possivelmente acumulados),
      NÃO faz clawback automático: emite alerta aos admins para tratativa manual.

    NÃO commita. Retorna um dict com o resumo do que foi feito (para observabilidade
    e testes). Idempotente: chamada repetida para o mesmo estorno não reverte 2×.
    """
    result = {
        "reversed_credits": 0,
        "consumed_walks": 0,
        "ledger_reversed": False,
        "alert_emitted": False,
    }

    cycle_ref = None
    cps = getattr(subscription, "current_period_start", None)
    if cps is not None:
        cycle_ref = cps.date().isoformat()

    # Idempotência: se já reverteu este ciclo, é no-op total.
    if credit_ledger_enabled() and _reversal_exists(db, subscription.id, cycle_ref):
        logger.info(
            "credit_refund: reversão já registrada (idempotente) subscription_id=%s cycle=%s",
            subscription.id, cycle_ref,
        )
        return result

    # 1) Créditos ainda não consumidos → zera atomicamente (só quem tem crédito vence).
    remaining_before = int(subscription.credits_remaining or 0)
    if remaining_before > 0:
        upd = db.execute(
            sa_update(TutorSubscription)
            .where(
                TutorSubscription.id == subscription.id,
                TutorSubscription.credits_remaining > 0,
            )
            .values(credits_remaining=0, updated_at=datetime.utcnow())
            .returning(TutorSubscription.credits_remaining)
        ).first()
        if upd is not None:
            result["reversed_credits"] = remaining_before
            subscription.credits_remaining = 0  # sincroniza objeto em memória

    # 2) Passeios de rede já consumidos (ambíguo) → alerta, sem clawback automático.
    consumed = _consumed_network_walks(db, subscription.id)
    result["consumed_walks"] = len(consumed)

    # 3) Ledger contábil: reversão do passivo (total negativo), idempotente por ciclo.
    if credit_ledger_enabled():
        try:
            unit = _unit_value(subscription)
            # total negativo: reverte o passivo pelos créditos que foram anulados.
            total = to_float(
                q2(to_money(result["reversed_credits"]) * to_money(unit)) * to_money(-1)
            )
            entry = CreditLedgerEntry(
                tenant_id=subscription.tenant_id,
                subscription_id=subscription.id,
                event_type=LEDGER_LIABILITY_REVERSED,
                cycle_reference=cycle_ref,
                credits_count=result["reversed_credits"],
                unit_value=unit,
                total_value=total,
                walk_id=None,
                payment_id=payment_id,
            )
            db.add(entry)
            result["ledger_reversed"] = True
        except Exception:
            logger.exception(
                "credit_refund: falha best-effort ao registrar reversão de ledger subscription_id=%s",
                subscription.id,
            )

    # 4) Alerta operacional só quando há caso ambíguo (passeios já consumidos).
    if consumed:
        _notify_admins_credit_refund(
            db, subscription,
            reason=reason,
            consumed_count=len(consumed),
            reversed_credits=result["reversed_credits"],
        )
        result["alert_emitted"] = True

    logger.info(
        "credit_refund: reverse_credit_purchase subscription_id=%s reason=%s "
        "reversed_credits=%d consumed_walks=%d ledger_reversed=%s alert=%s",
        subscription.id, reason, result["reversed_credits"], result["consumed_walks"],
        result["ledger_reversed"], result["alert_emitted"],
    )
    return result
