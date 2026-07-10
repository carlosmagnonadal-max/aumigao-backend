"""Motor único de cancelamento de passeio (mig 0107, R14 itens 3/4).

Spec: docs/superpowers/specs/2026-07-10-cancelamento-financeiro-design.md

Os 3 pontos de entrada (POST /walks/{id}/cancel, PUT /walks/{id}/status com
ride_cancelled vindo de tutor, admin PATCH /walks/{id}/status com
ride_cancelled) convergem em `process_tutor_cancellation`. Decide estorno
total/parcial via Asaas, compensação PENDENTE do walker, e grava o motivo —
tudo num único lugar, elimina a duplicação inline que existia em admin.py.

Princípios (money code, zero regressão):
- O cancelamento em si NUNCA é bloqueado por falha do gateway Asaas — o walk
  sempre transiciona para ride_cancelled; falha de refund vira refund_status
  "failed" + evento operacional para retry/visibilidade admin.
- Janela calculada em hora de PAREDE local do tenant via app.lib.walk_time
  (gotcha canônico de fuso — nunca comparar scheduled_date direto com utcnow).
- OPERATIONAL_LATE_CANCELLATION_MINUTES (telemetria de reliability, 60min) é
  uma janela PROPOSITALMENTE SEPARADA desta (billing, default 1440min) — ver
  operational_reliability_service.record_late_cancellation_if_applicable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.constants import PAID_PAYMENT_STATUSES
from app.core.money import q2, to_float, to_money
from app.models.payment import Payment
from app.models.walk import Walk

DEFAULT_FREE_WINDOW_MINUTES = 1440
DEFAULT_LATE_FEE_PERCENT = 50
DEFAULT_WALKER_SHARE_PERCENT = 100
DEFAULT_AUTO_REFUND_ON_CANCEL = True


@dataclass(frozen=True)
class CancellationConfig:
    free_window_minutes: int
    late_fee_percent: float
    walker_share_percent: float
    auto_refund_on_cancel: bool


def get_tenant_cancellation_config(db: Session, tenant_id: str | None) -> CancellationConfig:
    """Config por tenant (doutrina: tudo configurável, admin decide).

    Fallback de fábrica (24h/50%/100%/auto ON) quando o tenant não tem
    TenantSettings (relationship nullable) ou tenant_id é None — mesmo padrão
    de fallback de app.lib.walk_time.tenant_tz_name.
    """
    if tenant_id:
        try:
            from app.models.tenant import TenantSettings

            row = db.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
            if row is not None:
                return CancellationConfig(
                    free_window_minutes=int(row.cancellation_free_window_minutes or DEFAULT_FREE_WINDOW_MINUTES),
                    late_fee_percent=float(row.late_cancellation_fee_percent or 0),
                    walker_share_percent=float(row.late_fee_walker_share_percent or 0),
                    auto_refund_on_cancel=bool(row.auto_refund_on_cancel),
                )
        except Exception:
            pass
    return CancellationConfig(
        free_window_minutes=DEFAULT_FREE_WINDOW_MINUTES,
        late_fee_percent=DEFAULT_LATE_FEE_PERCENT,
        walker_share_percent=DEFAULT_WALKER_SHARE_PERCENT,
        auto_refund_on_cancel=DEFAULT_AUTO_REFUND_ON_CANCEL,
    )


def _confirmed_payment_for_walk(db: Session, walk_id: str) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.walk_id == walk_id, Payment.status.in_(PAID_PAYMENT_STATUSES))
        .order_by(Payment.created_at.desc())
        .first()
    )


def _is_late_cancellation(db: Session, walk: Walk, window_minutes: int, now: datetime) -> bool:
    """True = dentro da janela (cobra taxa). False = fora da janela (grátis).

    Fallback defensivo: scheduled_date ilegível → NÃO cobra taxa (não há base
    válida para justificar a retenção). Na prática scheduled_date é campo
    obrigatório desde a criação do walk — este ramo é virtualmente inalcançável.
    """
    from app.lib.walk_time import tenant_tz_name, walk_start_utc

    scheduled_utc = walk_start_utc(walk.scheduled_date, tenant_tz_name(db, walk.tenant_id))
    if scheduled_utc is None:
        return False
    return now >= scheduled_utc - timedelta(minutes=window_minutes)


async def process_tutor_cancellation(
    db: Session,
    walk: Walk,
    *,
    actor_role: str,
    actor_id: str | None = None,
    reason_type: str | None = None,
    reason_text: str | None = None,
    notify_tutor: bool = False,
) -> dict:
    """Motor único de cancelamento — chamado pelos 3 pontos de entrada.

    Não faz commit (caller comita) — segue o padrão dos demais handlers de
    walks.py/admin.py que commitam uma vez ao final da rota.

    Retorna um resumo (dict) do que foi decidido — útil para resposta de API e
    asserts de teste: {refund_kind, refund_status, refunded_amount,
    retained_amount, compensation_amount, walker_compensated}.
    """
    now = datetime.utcnow()
    config = get_tenant_cancellation_config(db, walk.tenant_id)
    is_late = _is_late_cancellation(db, walk, config.free_window_minutes, now)
    walker_id = walk.walker_id or walk.assigned_walker_id

    retained_amount = to_money(0)
    summary: dict = {
        "is_late": is_late,
        "refund_kind": None,       # None | "credit" | "total" | "partial"
        "refund_status": None,     # None | "pending" | "failed"
        "refunded_amount": None,
        "retained_amount": 0.0,
        "compensation_amount": 0.0,
        "walker_compensated": False,
    }

    if walk.subscription_id:
        # Passeio de assinatura (crédito) — sem Payment próprio (a cobrança é da
        # mensalidade). >janela devolve o crédito; <janela retém (é a própria
        # multa) e a compensação do walker é calculada sobre walk.price.
        summary["refund_kind"] = "credit"
        if not is_late:
            from app.services.recurring_plan_service import refund_credit_for_walk

            refund_credit_for_walk(db, walk)
        else:
            retained_amount = q2(to_money(walk.price) * to_money(config.late_fee_percent) / to_money(100))
    else:
        payment = _confirmed_payment_for_walk(db, walk.id)
        if payment and config.auto_refund_on_cancel:
            from app.routes.payments import refund_asaas_charge

            if not is_late:
                summary["refund_kind"] = "total"
                ok = await refund_asaas_charge(payment.provider, payment.provider_payment_id)
                payment.refund_status = "pending" if ok else "failed"
                if ok:
                    payment.refunded_amount = payment.amount
                    summary["refunded_amount"] = payment.amount
                summary["refund_status"] = payment.refund_status
                if not ok:
                    _record_refund_failure(db, walk, payment, reason="refund_total_falhou")
            else:
                retained_amount = q2(to_money(payment.amount) * to_money(config.late_fee_percent) / to_money(100))
                refund_value = to_float(q2(to_money(payment.amount) - retained_amount))
                summary["refund_kind"] = "partial"
                if refund_value > 0:
                    ok = await refund_asaas_charge(payment.provider, payment.provider_payment_id, value=refund_value)
                else:
                    # Taxa de 100% — nada a estornar (retenção integral); não chama o gateway.
                    ok = True
                payment.refund_status = "pending" if ok else "failed"
                if ok:
                    payment.refunded_amount = refund_value
                    summary["refunded_amount"] = refund_value
                summary["refund_status"] = payment.refund_status
                if not ok:
                    _record_refund_failure(db, walk, payment, reason="refund_parcial_falhou")
            db.add(payment)
        elif payment and not config.auto_refund_on_cancel:
            # auto_refund OFF: cancela e grava o que seria retido, mas NÃO chama o
            # gateway — fica para o admin processar manualmente.
            summary["refund_kind"] = "total" if not is_late else "partial"
            payment.refund_status = "pending"
            db.add(payment)
            if is_late:
                retained_amount = q2(to_money(payment.amount) * to_money(config.late_fee_percent) / to_money(100))
        # payment ausente (grátis/não pago): nada a estornar — walk só cancela.

    summary["retained_amount"] = to_float(retained_amount)

    # ── Compensação do walker (só se havia walker designado/aceito) ───────────
    if walker_id and retained_amount > 0 and config.walker_share_percent > 0:
        compensation = q2(retained_amount * to_money(config.walker_share_percent) / to_money(100))
        if compensation > 0:
            _create_compensation_review(db, walk, walker_id=walker_id, amount=to_float(compensation), reason_text=reason_text)
            summary["compensation_amount"] = to_float(compensation)
            summary["walker_compensated"] = True

    # ── Estado do walk ─────────────────────────────────────────────────────
    walk.operational_status = "ride_cancelled"
    walk.status = "Cancelado"
    walk.cancellation_reason_type = reason_type
    walk.cancellation_reason = reason_text
    walk.cancelled_at = now
    walk.cancelled_by_role = actor_role
    walk.matching_finished_at = walk.matching_finished_at or now
    walk.confirmation_expires_at = None
    db.add(walk)

    from app.services.operational_matching_service import log_event

    log_event(
        db,
        walk.id,
        "ride_cancelled",
        actor_type=actor_role,
        actor_id=actor_id,
        metadata={
            "reason_type": reason_type,
            "reason_text": reason_text,
            "is_late": is_late,
            "refund_kind": summary["refund_kind"],
            "refund_status": summary["refund_status"],
            "retained_amount": summary["retained_amount"],
            "compensation_amount": summary["compensation_amount"],
        },
    )

    from app.services.admin_operational_event_service import record_admin_operational_event

    record_admin_operational_event(
        db,
        event_type="walk_cancelled",
        entity_type="walk",
        entity_id=walk.id,
        severity="info" if not is_late else "medium",
        title="Passeio cancelado" + (" (tardio)" if is_late else ""),
        description=reason_text or "Cancelamento sem motivo informado.",
        source=f"cancel_walk_service:{actor_role}",
        metadata={
            "reason_type": reason_type,
            "is_late": is_late,
            "refund_kind": summary["refund_kind"],
            "retained_amount": summary["retained_amount"],
            "compensation_amount": summary["compensation_amount"],
        },
    )

    _notify_walker_and_tutor(db, walk, walker_id=walker_id, is_late=is_late, notify_tutor=notify_tutor)

    from app.services.operational_reliability_service import record_late_cancellation_if_applicable

    record_late_cancellation_if_applicable(walk, db)

    return summary


def _record_refund_failure(db: Session, walk: Walk, payment: Payment, *, reason: str) -> None:
    """Best-effort: NUNCA propaga — falha de log não pode perder o cancelamento."""
    try:
        from app.services.operational_observability_service import record_operational_log

        record_operational_log(
            db,
            event_type="cancel_refund_failed",
            severity="error",
            source="cancel_walk_service",
            message="Falha ao solicitar estorno no Asaas durante cancelamento — revisar manualmente.",
            context={"walk_id": walk.id, "payment_id": payment.id, "reason": reason},
        )
    except Exception:
        pass


def _create_compensation_review(db: Session, walk: Walk, *, walker_id: str, amount: float, reason_text: str | None) -> None:
    """Cria a revisão de compensação PENDENTE na MESMA fila das finalizações.

    Reusa WalkCompletionReview (kind="cancellation_compensation") — aparece em
    GET /admin/walk-completions/pending junto das finalizações normais; ao
    aprovar, admin.approve_walk_completion cria o WalkerEarning (ver ramo
    kind na rota) em vez de marcar o walk como concluído.
    """
    from uuid import uuid4

    from app.models.walk_completion_review import WalkCompletionReview

    db.add(
        WalkCompletionReview(
            id=str(uuid4()),
            tenant_id=walk.tenant_id,
            walk_id=walk.id,
            walker_user_id=walker_id,
            tutor_user_id=walk.tutor_id,
            status="pending_review",
            kind="cancellation_compensation",
            notes=(
                f"Compensação por cancelamento tardio do passeio. "
                f"Motivo do tutor: {reason_text}" if reason_text else
                "Compensação por cancelamento tardio do passeio."
            ),
            compensation_amount=amount,
        )
    )


def _notify_walker_and_tutor(db: Session, walk: Walk, *, walker_id: str | None, is_late: bool, notify_tutor: bool) -> None:
    """Notificação in-app + PUSH (via CRITICAL_WALK_STATUS_ACTIONS) para o
    walker designado, em TODO cancelamento — antes NINGUÉM era avisado."""
    from app.services.operational_matching_service import notify_tutor_walk_event, notify_walker_walk_event

    if walker_id:
        notify_walker_walk_event(
            db,
            walk,
            walker_id,
            title="Passeio cancelado",
            message=(
                "O passeio foi cancelado pelo tutor." if not is_late
                else "O passeio foi cancelado próximo ao horário — uma compensação está em análise."
            ),
            notification_type="walk_status",
            priority="high",
            action="ride_cancelled",
            metadata={"is_late": is_late},
        )
    if notify_tutor and walk.tutor_id:
        notify_tutor_walk_event(
            db,
            walk,
            title="Passeio cancelado",
            message="O passeio foi cancelado pela equipe operacional.",
            notification_type="walk_status",
            priority="high",
            action="ride_cancelled",
        )
