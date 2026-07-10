"""Modelos de alerta de custo (mig 0106) — criação e unicidade anti-duplicata."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_cost_alert_defaults():
    from app.models.cost_alert import CostAlert
    db = _db()
    alert = CostAlert(id="a1", tenant_id="t1", name="Mensal", budget_amount=500.0)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    assert alert.owner_type == "tenant"
    assert alert.scope == "total"
    assert alert.currency == "BRL"
    assert alert.period == "monthly"
    assert alert.evaluation == "both"
    assert alert.status == "active"
    assert alert.config_version == 1


def test_cost_alert_event_unique_dedupe():
    from app.models.cost_alert import CostAlertEvent
    db = _db()
    row = dict(tenant_id="t1", alert_id="a1", period_key="2026-07", threshold=80,
               kind="actual", config_version=1, spend_amount=400.0, budget_amount=500.0)
    db.add(CostAlertEvent(id="e1", **row))
    db.commit()
    db.add(CostAlertEvent(id="e2", **row))
    with pytest.raises(IntegrityError):
        db.commit()
