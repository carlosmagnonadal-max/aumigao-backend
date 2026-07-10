"""Alertas de custo do tenant (fase 1). Regra: alerta NOTIFICA, nunca bloqueia.

Permissões: get_admin_tenant_scope no TOPO de toda rota (regra do projeto) —
admin do tenant opera só o próprio tenant; super_admin global usa ?tenant_id=.
Acesso cruzado responde 404 (não revela existência).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.cost_alert import (
    ALERT_CHANNELS,
    ALERT_EVALUATIONS,
    ALERT_PERIODS,
    ALERT_SCOPES,
    ALERT_STATUS_ACTIVE,
    ALERT_STATUS_PAUSED,
    CostAlert,
    CostAlertEvent,
)
from app.models.user import User
# copiar do topo de app/routes/admin.py:
from app.dependencies.tenant_scope import get_admin_tenant_scope
from app.dependencies.auth import get_current_user

api_router = APIRouter(prefix="/api/admin/cost-alerts", tags=["cost-alerts"])


class CostAlertPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scope: str = "total"
    budget_amount: float = Field(gt=0)
    period: str = "monthly"
    thresholds: list[int] = Field(min_length=1, max_length=5)
    evaluation: str = "both"
    channels: list[str] = ["in_app"]

    @field_validator("scope")
    @classmethod
    def _scope(cls, v):
        if v not in ALERT_SCOPES:
            raise ValueError("Escopo inválido.")
        return v

    @field_validator("period")
    @classmethod
    def _period(cls, v):
        if v not in ALERT_PERIODS:
            raise ValueError("Período inválido.")
        return v

    @field_validator("evaluation")
    @classmethod
    def _evaluation(cls, v):
        if v not in ALERT_EVALUATIONS:
            raise ValueError("Tipo de avaliação inválido.")
        return v

    @field_validator("thresholds")
    @classmethod
    def _thresholds(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("Thresholds duplicados.")
        if any(t < 1 or t > 200 for t in v):
            raise ValueError("Thresholds devem estar entre 1% e 200%.")
        return sorted(v)

    @field_validator("channels")
    @classmethod
    def _channels(cls, v):
        if any(c not in ALERT_CHANNELS for c in v):
            raise ValueError("Canal inválido.")
        if "in_app" not in v:
            v = ["in_app", *v]
        return v


def _require_admin(user: User):
    if user.role not in ("admin", "super_admin"):
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores.")


def _resolve_tenant_id(user: User, db: Session, tenant_id_param: str | None) -> str:
    scope = get_admin_tenant_scope(user, db)
    target = scope.tenant_id or tenant_id_param
    if not target:
        raise HTTPException(status_code=400, detail="tenant_id obrigatório para admin global.")
    return target


def _get_owned_alert(db: Session, tenant_id: str, alert_id: str) -> CostAlert:
    alert = db.get(CostAlert, alert_id)
    if not alert or alert.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Alerta não encontrado.")
    return alert


def _serialize(db: Session, alert: CostAlert, *, with_status: bool = True, tz_name: str | None = None) -> dict:
    from decimal import Decimal
    from app.lib.walk_time import tenant_tz_name
    from app.services.cost_alert_service import forecast_amount, period_window, tenant_spend

    data = {
        "id": alert.id, "tenant_id": alert.tenant_id, "name": alert.name,
        "scope": alert.scope, "currency": alert.currency,
        "budget_amount": float(alert.budget_amount), "period": alert.period,
        "thresholds": json.loads(alert.thresholds_json or "[]"),
        "evaluation": alert.evaluation,
        "channels": json.loads(alert.channels_json or '["in_app"]'),
        "status": alert.status, "config_version": alert.config_version,
        "created_at": alert.created_at, "updated_at": alert.updated_at,
    }
    if with_status:
        tz = tz_name or tenant_tz_name(db, alert.tenant_id)
        start, end, period_key, elapsed = period_window(alert.period, datetime.utcnow(), tz)
        spend = tenant_spend(db, alert.tenant_id, alert.scope, start, end)
        projected = forecast_amount(spend, elapsed)
        budget = Decimal(str(alert.budget_amount))
        percent = float(spend / budget * 100) if budget else 0.0
        next_threshold = next((t for t in data["thresholds"] if percent < t), None)
        data.update({
            "period_key": period_key,
            "current_spend": float(spend),
            "forecast": float(projected) if projected is not None else None,
            "percent_used": round(percent, 1),
            "next_threshold": next_threshold,
        })
    return data


@api_router.get("")
def list_alerts(
    tenant_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    target = _resolve_tenant_id(user, db, tenant_id)
    alerts = (
        db.query(CostAlert)
        .filter(CostAlert.tenant_id == target, CostAlert.owner_type == "tenant")
        .order_by(CostAlert.created_at.desc())
        .all()
    )
    # 1 tenant → 1 timezone: resolve uma vez em vez de por alerta (N+1 em tenant
    # com vários alertas configurados).
    from app.lib.walk_time import tenant_tz_name
    tz_name = tenant_tz_name(db, target)
    return [_serialize(db, alert, tz_name=tz_name) for alert in alerts]


@api_router.post("", status_code=201)
def create_alert(
    payload: CostAlertPayload,
    tenant_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    target = _resolve_tenant_id(user, db, tenant_id)
    alert = CostAlert(
        id=str(uuid.uuid4()), tenant_id=target, owner_type="tenant",
        name=payload.name.strip(), scope=payload.scope,
        budget_amount=payload.budget_amount, period=payload.period,
        thresholds_json=json.dumps(payload.thresholds),
        evaluation=payload.evaluation, channels_json=json.dumps(payload.channels),
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return _serialize(db, alert)


@api_router.put("/{alert_id}")
def update_alert(
    alert_id: str,
    payload: CostAlertPayload,
    tenant_id: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_admin(user)
    target = _resolve_tenant_id(user, db, tenant_id)
    alert = _get_owned_alert(db, target, alert_id)
    new_thresholds = json.dumps(payload.thresholds)
    config_changed = (
        float(alert.budget_amount) != float(payload.budget_amount)
        or alert.period != payload.period
        or alert.scope != payload.scope
        or alert.evaluation != payload.evaluation
        or (alert.thresholds_json or "[]") != new_thresholds
    )
    alert.name = payload.name.strip()
    alert.scope = payload.scope
    alert.budget_amount = payload.budget_amount
    alert.period = payload.period
    alert.thresholds_json = new_thresholds
    alert.evaluation = payload.evaluation
    alert.channels_json = json.dumps(payload.channels)
    if config_changed:
        alert.config_version = int(alert.config_version or 1) + 1
    db.commit()
    db.refresh(alert)
    return _serialize(db, alert)


@api_router.post("/{alert_id}/pause")
def pause_alert(alert_id: str, tenant_id: str | None = Query(None),
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    alert = _get_owned_alert(db, _resolve_tenant_id(user, db, tenant_id), alert_id)
    alert.status = ALERT_STATUS_PAUSED
    db.commit()
    db.refresh(alert)
    return _serialize(db, alert, with_status=False)


@api_router.post("/{alert_id}/resume")
def resume_alert(alert_id: str, tenant_id: str | None = Query(None),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    alert = _get_owned_alert(db, _resolve_tenant_id(user, db, tenant_id), alert_id)
    alert.status = ALERT_STATUS_ACTIVE
    db.commit()
    db.refresh(alert)
    return _serialize(db, alert, with_status=False)


@api_router.delete("/{alert_id}", status_code=204)
def delete_alert(alert_id: str, tenant_id: str | None = Query(None),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    target = _resolve_tenant_id(user, db, tenant_id)
    alert = _get_owned_alert(db, target, alert_id)
    db.query(CostAlertEvent).filter(CostAlertEvent.alert_id == alert.id).delete()
    db.delete(alert)
    db.commit()


@api_router.get("/{alert_id}/events")
def alert_events(alert_id: str, tenant_id: str | None = Query(None),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    target = _resolve_tenant_id(user, db, tenant_id)
    alert = _get_owned_alert(db, target, alert_id)
    events = (
        db.query(CostAlertEvent)
        .filter(CostAlertEvent.alert_id == alert.id)
        .order_by(CostAlertEvent.created_at.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": e.id, "period_key": e.period_key, "threshold": e.threshold,
            "kind": e.kind, "config_version": e.config_version,
            "spend_amount": float(e.spend_amount), "budget_amount": float(e.budget_amount),
            "channels": json.loads(e.channels_json or "[]"),
            "delivery": json.loads(e.delivery_json or "{}"),
            "created_at": e.created_at,
        }
        for e in events
    ]
