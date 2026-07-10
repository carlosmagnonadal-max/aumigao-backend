"""Motor dos alertas de custo (fase 1: tenant / commission_entries).

Lógica pura (period_window, forecast_amount, crossed_thresholds) separada do
acesso a banco (tenant_spend, evaluate_cost_alerts — próximo lote) para ser
testável sem DB. Dinheiro = Decimal fim-a-fim; períodos em fuso LOCAL do tenant
(mesma disciplina de app/lib/walk_time.py).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from sqlalchemy import func as sa_func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.commission_entry import COMM_ACCRUED, COMM_BILLED, COMM_PAID, CommissionEntry
from app.models.cost_alert import ALERT_STATUS_ACTIVE, CostAlert, CostAlertEvent

LOGGER = logging.getLogger("aumigao.cost_alerts")

_CENT = Decimal("0.01")
# Fração mínima do período decorrida para projetar (guarda contra divisão
# por ~zero e projeção absurda no início do período).
MIN_FORECAST_FRACTION = 0.10
DEFAULT_TZ = "America/Bahia"


def period_window(period: str, now_utc: datetime, tz_name: str | None) -> tuple[datetime, datetime, str, float]:
    """(start_utc, end_utc, period_key, elapsed_fraction) da janela que contém
    now_utc, calculada no fuso local do tenant. Datetimes retornam UTC naive
    (padrão do projeto). Semana = ISO (segunda a domingo)."""
    tz = ZoneInfo(tz_name or DEFAULT_TZ)
    local_now = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)

    if period == "daily":
        local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_end = local_start + timedelta(days=1)
        key = local_start.strftime("%Y-%m-%d")
    elif period == "weekly":
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        local_start = day_start - timedelta(days=local_now.weekday())
        local_end = local_start + timedelta(days=7)
        iso = local_start.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
    elif period == "monthly":
        local_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if local_start.month == 12:
            local_end = local_start.replace(year=local_start.year + 1, month=1)
        else:
            local_end = local_start.replace(month=local_start.month + 1)
        key = local_start.strftime("%Y-%m")
    else:
        raise ValueError(f"Período inválido: {period}")

    start_utc = local_start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = local_end.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    now_naive = now_utc.replace(tzinfo=None)
    total = (end_utc - start_utc).total_seconds()
    elapsed = max(0.0, min(1.0, (now_naive - start_utc).total_seconds() / total))
    return start_utc, end_utc, key, elapsed


def forecast_amount(spend: Decimal, elapsed_fraction: float) -> Decimal | None:
    """Projeção linear do gasto até o fim do período. None se o período mal
    começou (fração < MIN_FORECAST_FRACTION) — projetar cedo demais gera alarme falso."""
    if elapsed_fraction < MIN_FORECAST_FRACTION:
        return None
    return (spend / Decimal(str(elapsed_fraction))).quantize(_CENT, rounding=ROUND_HALF_UP)


def crossed_thresholds(
    *,
    spend: Decimal,
    budget: Decimal,
    thresholds: list[int],
    evaluation: str,
    elapsed_fraction: float,
) -> list[tuple[int, str]]:
    """Thresholds cruzados agora, como (threshold, kind). Regras:
    - actual: spend >= budget * T/100.
    - forecast: só T >= 100 (projeção interessa pra "vai estourar") e
      projeção >= budget * T/100.
    O dedupe de já-notificado NÃO é aqui — é o índice único de cost_alert_events."""
    hits: list[tuple[int, str]] = []
    projected = forecast_amount(spend, elapsed_fraction)
    for threshold in sorted(thresholds):
        limit = (budget * Decimal(threshold) / Decimal(100)).quantize(_CENT, rounding=ROUND_HALF_UP)
        if evaluation in ("actual", "both") and spend >= limit and spend > 0:
            hits.append((threshold, "actual"))
        if (
            evaluation in ("forecast", "both")
            and threshold >= 100
            and projected is not None
            and projected >= limit
            and spend > 0
        ):
            hits.append((threshold, "forecast"))
    return hits


_SPEND_STATUSES = (COMM_ACCRUED, COMM_BILLED, COMM_PAID)  # void fica fora


def tenant_spend(db: Session, tenant_id: str, scope: str, start_utc: datetime, end_utc: datetime) -> Decimal:
    """Soma da comissão medida do tenant na janela, por escopo. Decimal sempre."""
    query = (
        db.query(sa_func.coalesce(sa_func.sum(CommissionEntry.amount), 0))
        .filter(
            CommissionEntry.tenant_id == tenant_id,
            CommissionEntry.status.in_(_SPEND_STATUSES),
            CommissionEntry.created_at >= start_utc,
            CommissionEntry.created_at < end_utc,
        )
    )
    if scope == "own_walkers":
        query = query.filter(CommissionEntry.is_network.is_(False))
    elif scope == "network":
        query = query.filter(CommissionEntry.is_network.is_(True))
    return Decimal(str(query.scalar() or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def tutor_spend(db: Session, tutor_id: str, scope: str, start_utc: datetime, end_utc: datetime) -> Decimal:
    """Gasto do TUTOR na janela: payments com status pago. Escopo:
    total = todos os pagamentos do tutor; pet:{id} = só os de walks daquele pet
    (pagamento sem walk — ex. assinatura — só entra no total)."""
    from app.constants import PAID_PAYMENT_STATUSES
    from app.models.payment import Payment
    from app.models.walk import Walk as WalkModel

    query = (
        db.query(sa_func.coalesce(sa_func.sum(Payment.amount), 0))
        .filter(
            Payment.tutor_id == tutor_id,
            Payment.status.in_(list(PAID_PAYMENT_STATUSES)),
            Payment.created_at >= start_utc,
            Payment.created_at < end_utc,
        )
    )
    if scope.startswith("pet:"):
        pet_id = scope.split(":", 1)[1]
        query = query.join(WalkModel, WalkModel.id == Payment.walk_id).filter(WalkModel.pet_id == pet_id)
    return Decimal(str(query.scalar() or 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


_PERIOD_LABEL = {"daily": "hoje", "weekly": "esta semana", "monthly": "este mês"}
_SCOPE_LABEL = {"total": "custo total", "own_walkers": "passeadores próprios", "network": "rede compartilhada"}


def _notify_cost_alert(db: Session, alert: CostAlert, event: CostAlertEvent) -> dict:
    """Fan-out best-effort pelos canais do alerta. Retorna delivery por canal.
    Falha de um canal não impede os outros nem o registro do evento."""
    channels = json.loads(alert.channels_json or '["in_app"]')
    if "in_app" not in channels:
        channels.insert(0, "in_app")
    percent = int(round(float(Decimal(str(event.spend_amount)) / Decimal(str(alert.budget_amount)) * 100)))
    kind_label = "custo real" if event.kind == "actual" else "projeção"
    title = f"💰 Alerta de custo: {alert.name} atingiu {event.threshold}%"
    message = (
        f"O {_SCOPE_LABEL.get(alert.scope, alert.scope)} {_PERIOD_LABEL.get(alert.period, alert.period)} "
        f"chegou a R$ {float(event.spend_amount):.2f} de um orçamento de R$ {float(alert.budget_amount):.2f} "
        # Refere-se ao THRESHOLD cruzado (event.threshold), não ao percent real
        # (spend/budget) — com vários thresholds disparando na mesma avaliação, o
        # percent real seria idêntico em todos os eventos e confundiria qual limite
        # foi cruzado. percent real fica só nos metadados, p/ precisão.
        f"({event.threshold}% — {kind_label}). Veja os detalhes em Financeiro › Alertas de custo."
    )
    delivery: dict[str, str] = {}

    from app.models.user import User
    admins = (
        db.query(User)
        .filter(User.role.in_(["admin", "super_admin"]), User.tenant_id == alert.tenant_id)
        .all()
    )
    try:
        from app.routes.notifications import NotificationCreate, _create_notification
        for admin in admins:
            _create_notification(db, NotificationCreate(
                user_id=admin.id,
                user_role=admin.role,
                tenant_id=alert.tenant_id,
                title=title,
                message=message,
                type="cost_alert",
                related_entity_type="cost_alert",
                related_entity_id=alert.id,
                metadata={
                    "threshold": event.threshold, "kind": event.kind,
                    "period_key": event.period_key, "percent": percent,
                },
            ))
        delivery["in_app"] = "sent"
        if "push" in channels:
            delivery["push"] = "sent"  # push sai pela própria _create_notification (tipo na whitelist)
    except Exception:
        LOGGER.exception("cost_alert: falha in-app alert_id=%s", alert.id)
        delivery["in_app"] = "failed"

    if "email" in channels:
        try:
            from app.services.transactional_email_service import send_cost_alert_email
            results = [
                send_cost_alert_email(admin.email, title, message, db=db, tenant_id=alert.tenant_id)
                for admin in admins if admin.email
            ]
            # send_cost_alert_email nunca levanta (fire-safe) — o retorno é que
            # diz se realmente saiu. "sent" só se TODOS os envios confirmaram.
            delivery["email"] = "sent" if all(results) else "failed"
        except Exception:
            LOGGER.exception("cost_alert: falha email alert_id=%s", alert.id)
            delivery["email"] = "failed"
    return delivery


def _notify_tutor_cost_alert(db: Session, alert: CostAlert, event: CostAlertEvent) -> dict:
    """Fan-out do alerta do TUTOR: notificação direta ao dono (user_id) — o push
    sai pela própria _create_notification (tipo cost_alert já está na whitelist).
    E-mail opcional pro e-mail do tutor. Espelha _notify_cost_alert (admins)."""
    channels = json.loads(alert.channels_json or '["in_app"]')
    if "in_app" not in channels:
        channels.insert(0, "in_app")
    percent = int(round(float(Decimal(str(event.spend_amount)) / Decimal(str(alert.budget_amount)) * 100)))
    kind_label = "gasto real" if event.kind == "actual" else "projeção"
    title = f"💰 Orçamento: {alert.name} atingiu {event.threshold}%"
    message = (
        f"Seus gastos {_PERIOD_LABEL.get(alert.period, alert.period)} chegaram a "
        f"R$ {float(event.spend_amount):.2f} de um orçamento de R$ {float(alert.budget_amount):.2f} "
        f"({percent}% — {kind_label}). Veja em Orçamento de gastos."
    )
    delivery: dict[str, str] = {}
    try:
        from app.routes.notifications import NotificationCreate, _create_notification
        _create_notification(db, NotificationCreate(
            user_id=alert.owner_user_id,
            user_role="tutor",
            tenant_id=alert.tenant_id,
            title=title,
            message=message,
            type="cost_alert",
            related_entity_type="cost_alert",
            related_entity_id=alert.id,
            metadata={"threshold": event.threshold, "kind": event.kind,
                      "period_key": event.period_key, "percent": percent},
        ))
        delivery["in_app"] = "sent"
        if "push" in channels:
            delivery["push"] = "sent"
    except Exception:
        LOGGER.exception("cost_alert: falha in-app tutor alert_id=%s", alert.id)
        delivery["in_app"] = "failed"

    if "email" in channels:
        try:
            from app.models.user import User as UserModel
            from app.services.transactional_email_service import send_cost_alert_email
            owner = db.get(UserModel, alert.owner_user_id)
            ok = bool(owner and owner.email) and send_cost_alert_email(
                owner.email, title, message, db=db, tenant_id=alert.tenant_id)
            delivery["email"] = "sent" if ok else "failed"
        except Exception:
            LOGGER.exception("cost_alert: falha email tutor alert_id=%s", alert.id)
            delivery["email"] = "failed"
    return delivery


def evaluate_cost_alerts(db: Session, now_utc: datetime | None = None) -> int:
    """Avalia alertas ativos; INSERT do evento é o dedupe (índice único —
    conflito = já notificado neste período/threshold/kind/config). Retorna
    quantos eventos NOVOS dispararam."""
    from app.lib.walk_time import tenant_tz_name

    now = now_utc or datetime.utcnow()
    fired = 0
    alerts = db.query(CostAlert).filter(
        CostAlert.status == ALERT_STATUS_ACTIVE,
    ).all()
    for alert in alerts:
        try:
            if alert.owner_type == "tutor" and not alert.owner_user_id:
                # Defensivo: alerta "tutor" sem dono não tem pra quem notificar
                # (mesmo estilo da guarda de budget invalido, abaixo).
                LOGGER.warning("cost_alert: tutor sem owner_user_id alert_id=%s", alert.id)
                continue
            tz = tenant_tz_name(db, alert.tenant_id)
            start, end, period_key, elapsed = period_window(alert.period, now, tz)
            if alert.owner_type == "tutor" and alert.owner_user_id:
                spend = tutor_spend(db, alert.owner_user_id, alert.scope, start, end)
            else:
                spend = tenant_spend(db, alert.tenant_id, alert.scope, start, end)
            budget = Decimal(str(alert.budget_amount))
            if budget <= 0:
                # Defesa contra alerta criado fora do Pydantic (fase 2/scripts) —
                # a rota já valida budget_amount > 0, mas o modelo não impõe isso.
                LOGGER.warning("cost_alert: budget invalido alert_id=%s budget=%s", alert.id, budget)
                continue
            thresholds = [int(t) for t in json.loads(alert.thresholds_json or "[]")]
            hits = crossed_thresholds(
                spend=spend, budget=budget, thresholds=thresholds,
                evaluation=alert.evaluation, elapsed_fraction=elapsed,
            )
            LOGGER.info(
                "cost_alert evaluated alert_id=%s period_key=%s spend=%s budget=%s hits=%d",
                alert.id, period_key, spend, budget, len(hits),
            )
            for threshold, kind in hits:
                event = CostAlertEvent(
                    id=str(uuid.uuid4()), tenant_id=alert.tenant_id, alert_id=alert.id,
                    period_key=period_key, threshold=threshold, kind=kind,
                    config_version=alert.config_version,
                    spend_amount=float(spend), budget_amount=float(budget),
                    channels_json=alert.channels_json or '["in_app"]',
                )
                nested = db.begin_nested()
                try:
                    db.add(event)
                    nested.commit()
                except IntegrityError:
                    nested.rollback()  # já disparado neste período/config → silêncio
                    continue
                notifier = _notify_tutor_cost_alert if alert.owner_type == "tutor" else _notify_cost_alert
                delivery = notifier(db, alert, event)
                event.delivery_json = json.dumps(delivery)
                db.commit()
                fired += 1
                LOGGER.warning(
                    "cost_alert TRIGGERED alert_id=%s threshold=%s kind=%s period_key=%s",
                    alert.id, threshold, kind, period_key,
                )
        except Exception:
            LOGGER.exception("cost_alert: falha ao avaliar alert_id=%s", alert.id)
            db.rollback()
    db.commit()
    return fired
