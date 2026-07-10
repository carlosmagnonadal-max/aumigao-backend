"""Alertas de custo (mig 0106) — motor genérico tenant/tutor. Fase 1: tenant.

CostAlert = orçamento com thresholds. CostAlertEvent = disparo registrado; o
índice único (alert_id, period_key, threshold, kind, config_version) é a
garantia anti-duplicata — INSERT conflita = já notificado neste período/config.
Alertas NUNCA bloqueiam consumo; apenas notificam.
"""
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.types import Money

ALERT_STATUS_ACTIVE = "active"
ALERT_STATUS_PAUSED = "paused"
ALERT_PERIODS = ("daily", "weekly", "monthly")
ALERT_SCOPES = ("total", "own_walkers", "network")
ALERT_EVALUATIONS = ("actual", "forecast", "both")
ALERT_CHANNELS = ("in_app", "push", "email")


class CostAlert(Base):
    __tablename__ = "cost_alerts"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    owner_type = Column(String, nullable=False, default="tenant")
    owner_user_id = Column(String, nullable=True)  # fase 2 (tutor)

    name = Column(String(120), nullable=False)
    scope = Column(String, nullable=False, default="total")
    currency = Column(String, nullable=False, default="BRL")
    budget_amount = Column(Money, nullable=False)
    period = Column(String, nullable=False, default="monthly")
    thresholds_json = Column(String, nullable=False, default="[50, 80, 100]")
    evaluation = Column(String, nullable=False, default="both")
    channels_json = Column(String, nullable=False, default='["in_app"]')

    status = Column(String, nullable=False, default=ALERT_STATUS_ACTIVE, index=True)
    config_version = Column(Integer, nullable=False, default=1)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CostAlertEvent(Base):
    __tablename__ = "cost_alert_events"
    __table_args__ = (
        UniqueConstraint("alert_id", "period_key", "threshold", "kind", "config_version",
                         name="uq_cost_alert_events_dedupe"),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    alert_id = Column(String, nullable=False, index=True)
    period_key = Column(String, nullable=False)
    threshold = Column(Integer, nullable=False)
    kind = Column(String, nullable=False)  # "actual" | "forecast"
    config_version = Column(Integer, nullable=False)
    spend_amount = Column(Money, nullable=False)
    budget_amount = Column(Money, nullable=False)
    channels_json = Column(String, nullable=False, default="[]")
    delivery_json = Column(String, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
