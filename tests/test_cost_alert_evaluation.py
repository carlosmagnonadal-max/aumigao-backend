"""Avaliação de alertas: gasto real, dedupe por índice único, re-disparo por
config_version/período novo, notificação in-app criada, push na whitelist."""
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import CommissionEntry
from app.models.cost_alert import CostAlert, CostAlertEvent
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.services.cost_alert_service import evaluate_cost_alerts, tenant_spend
from decimal import Decimal

TENANT = "t-cost"


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.info["rls_tenant"] = "*"
    db.add(Tenant(id=TENANT, name="T", slug="t-cost", status="active", plan="business"))
    db.add(User(id="adm-1", email="adm@t.com", password_hash="x", role="admin", tenant_id=TENANT))
    db.commit()
    return db


def _entry(db, *, amount, is_network=False, walk_id=None, created_at=None):
    entry = CommissionEntry(
        id=f"ce-{walk_id}", tenant_id=TENANT, walk_id=walk_id, period="2026-07",
        walk_price=amount * 10, commission_percent=10.0, amount=amount,
        is_network=is_network, status="accrued",
    )
    db.add(entry)
    db.commit()
    if created_at is not None:
        db.query(CommissionEntry).filter(CommissionEntry.id == entry.id).update({"created_at": created_at})
        db.commit()
    return entry


def _alert(db, *, budget=100.0, thresholds="[80, 100]", evaluation="actual", scope="total", period="monthly"):
    alert = CostAlert(id="al-1", tenant_id=TENANT, name="Mensal", scope=scope,
                      budget_amount=budget, period=period, thresholds_json=thresholds,
                      evaluation=evaluation, channels_json='["in_app"]')
    db.add(alert)
    db.commit()
    return alert


def test_tenant_spend_scopes_and_window():
    db = _db()
    now = datetime.utcnow()
    _entry(db, amount=30.0, walk_id="w1")
    _entry(db, amount=20.0, walk_id="w2", is_network=True)
    _entry(db, amount=99.0, walk_id="w-old", created_at=now - timedelta(days=90))
    start, end = now - timedelta(days=30), now + timedelta(days=1)
    assert tenant_spend(db, TENANT, "total", start, end) == Decimal("50.00")
    assert tenant_spend(db, TENANT, "own_walkers", start, end) == Decimal("30.00")
    assert tenant_spend(db, TENANT, "network", start, end) == Decimal("20.00")


def test_evaluate_triggers_once_and_dedupes():
    db = _db()
    _alert(db, budget=100.0, thresholds="[80]")
    _entry(db, amount=85.0, walk_id="w1")
    assert evaluate_cost_alerts(db) == 1
    assert evaluate_cost_alerts(db) == 0  # dedupe: mesmo período/threshold/config
    events = db.query(CostAlertEvent).all()
    assert len(events) == 1
    assert events[0].threshold == 80 and events[0].kind == "actual"
    notif = db.query(Notification).filter(Notification.type == "cost_alert").all()
    assert len(notif) == 1
    assert "80%" in notif[0].message


def test_refires_after_config_version_bump():
    db = _db()
    alert = _alert(db, budget=100.0, thresholds="[80]")
    _entry(db, amount=85.0, walk_id="w1")
    assert evaluate_cost_alerts(db) == 1
    alert.config_version = 2
    db.commit()
    assert evaluate_cost_alerts(db) == 1  # config nova → notifica de novo


def test_paused_alert_not_evaluated():
    db = _db()
    alert = _alert(db, budget=100.0, thresholds="[80]")
    alert.status = "paused"
    db.commit()
    _entry(db, amount=85.0, walk_id="w1")
    assert evaluate_cost_alerts(db) == 0


def test_cost_alert_push_type_is_critical():
    from app.services.push_notifications import CRITICAL_NOTIFICATION_TYPES
    assert "cost_alert" in CRITICAL_NOTIFICATION_TYPES


def test_email_delivery_reflects_real_failure(monkeypatch):
    """Regressão: send_cost_alert_email engolia a exceção e sempre devolvia
    None, então delivery_json marcava "sent" mesmo com o transporte quebrado.
    Agora o retorno bool é honrado — falha real vira "failed", sem quebrar o
    in_app (que usa outro canal)."""
    import app.services.transactional_email_service as email_service

    def _boom(*args, **kwargs):
        raise RuntimeError("smtp indisponível")

    monkeypatch.setattr(email_service, "_send_email", _boom)

    db = _db()
    _alert(db, budget=100.0, thresholds="[80]", evaluation="actual")
    db.query(CostAlert).filter(CostAlert.id == "al-1").update({"channels_json": '["in_app", "email"]'})
    db.commit()
    _entry(db, amount=85.0, walk_id="w1")

    assert evaluate_cost_alerts(db) == 1
    event = db.query(CostAlertEvent).one()
    delivery = json.loads(event.delivery_json)
    assert delivery["email"] == "failed"
    assert delivery["in_app"] == "sent"


def test_refires_on_new_period_key():
    """Prova a promessa da spec: o dedupe é por (alert_id, period_key, threshold,
    kind, config_version) — uma virada de período (julho → agosto) libera novo
    disparo mesmo com config_version e threshold iguais."""
    db = _db()
    _alert(db, budget=100.0, thresholds="[80]")
    _entry(db, amount=85.0, walk_id="w1", created_at=datetime(2026, 7, 15))
    assert evaluate_cost_alerts(db, now_utc=datetime(2026, 7, 20)) == 1
    assert evaluate_cost_alerts(db, now_utc=datetime(2026, 7, 21)) == 0  # mesmo período → dedupe

    _entry(db, amount=85.0, walk_id="w2", created_at=datetime(2026, 8, 5))
    assert evaluate_cost_alerts(db, now_utc=datetime(2026, 8, 10)) == 1  # agosto = period_key novo
