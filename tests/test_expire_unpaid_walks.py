"""R7 — expiração de passeios não pagos (scheduler).

Passeios em 'awaiting_payment' criados há mais de WALK_PAYMENT_TIMEOUT_HOURS
(default 24h) são cancelados e o tutor é notificado. Idempotente: só toca walks
ainda à espera; não afeta os dentro do prazo nem os já em outros estados.
"""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.notification import Notification
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.services.operational_scheduler_service import _task_expire_unpaid_walks

# scheduled_date é hora de PAREDE local do tenant (default America/Bahia) —
# os horários dos testes são gerados no relógio local, como o app grava.
_TZ = ZoneInfo("America/Bahia")


def _local_now() -> datetime:
    return datetime.now(_TZ).replace(tzinfo=None)


def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Factory = sessionmaker(bind=eng)
    db = Factory()
    db.info["rls_tenant"] = "*"
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="pro"))
    db.add(User(id="tutor1", email="t@x.com", password_hash="x", role="cliente", tenant_id="t1"))
    db.commit()
    return Factory, db


def _future_iso(hours: int = 6) -> str:
    return (_local_now() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def _walk(db, wid, op_status, age_hours, scheduled_date=None):
    # Default scheduled_date bem no futuro → o corte de 45min NÃO se aplica;
    # só o timeout absoluto (age_hours) governa. Testes de corte passam data explícita.
    db.add(Walk(
        id=wid, tutor_id="tutor1", tenant_id="t1", pet_id="p1",
        scheduled_date=scheduled_date or _future_iso(),
        duration_minutes=30, price=50.0,
        status="aguardando_pagamento" if op_status == "awaiting_payment" else "Agendado",
        operational_status=op_status,
        created_at=datetime.utcnow() - timedelta(hours=age_hours),
    ))
    db.commit()


def _run(db):
    n = _task_expire_unpaid_walks(db)
    try:
        db.commit()
    except Exception:
        pass
    return n


def test_expires_only_overdue_awaiting_walks():
    Factory, db = _setup()
    _walk(db, "old", "awaiting_payment", age_hours=30)   # vencido (>24h)
    _walk(db, "fresh", "awaiting_payment", age_hours=2)   # dentro do prazo
    _walk(db, "scheduled", "pending_walker_confirmation", age_hours=48)  # já pago/agendado

    count = _run(db)
    assert count == 1

    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        old = db2.get(Walk, "old")
        assert old.operational_status == "ride_cancelled"
        assert old.status == "Cancelado"
        assert "pagamento" in (old.no_walker_reason or "").lower()
        # não vencido: intacto
        assert db2.get(Walk, "fresh").operational_status == "awaiting_payment"
        # já agendado: intacto
        assert db2.get(Walk, "scheduled").operational_status == "pending_walker_confirmation"
    finally:
        db2.close()


def test_notifies_tutor_on_expiry():
    Factory, db = _setup()
    _walk(db, "old", "awaiting_payment", age_hours=30)
    _run(db)

    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        notifs = db2.query(Notification).filter(Notification.user_id == "tutor1").all()
        assert len(notifs) == 1
        assert notifs[0].related_entity_id == "old"
    finally:
        db2.close()


def test_idempotent_second_run_is_noop():
    Factory, db = _setup()
    _walk(db, "old", "awaiting_payment", age_hours=30)
    assert _run(db) == 1
    # segunda passada: já cancelado, não toca mais nada
    assert _run(db) == 0


def test_cutoff_45min_expires_walk_near_start(monkeypatch):
    """Corte de 45min: passeio FRESCO (dentro do timeout de 24h) mas cujo início
    está a menos de 45min de agora é expirado — não dá mais tempo de executar."""
    Factory, db = _setup()
    # criado agora (2h), início a 10min → dentro do corte de 45min.
    near_start = (_local_now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M")
    _walk(db, "near", "awaiting_payment", age_hours=1, scheduled_date=near_start)
    # início bem no futuro (6h) → NÃO deve expirar.
    _walk(db, "far", "awaiting_payment", age_hours=1)

    assert _run(db) == 1
    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        assert db2.get(Walk, "near").operational_status == "ride_cancelled"
        assert "corte" in (db2.get(Walk, "near").no_walker_reason or "").lower()
        assert db2.get(Walk, "far").operational_status == "awaiting_payment"
    finally:
        db2.close()


def test_regression_local_time_nao_e_utc_bug_2026_07_08():
    """REGRESSÃO do bug de 08/07/2026: passeio criado às 9:21 locais com início
    às 10:30 locais (69min no futuro) era cancelado em 1 minuto porque '10:30'
    era comparado como UTC (12:22 UTC > 10:30 'UTC'). Com a conversão de fuso
    (America/Bahia), o passeio está FORA do corte de 45min e NÃO pode expirar."""
    Factory, db = _setup()
    start_69 = (_local_now() + timedelta(minutes=69)).strftime("%Y-%m-%dT%H:%M")
    _walk(db, "carlos", "awaiting_payment", age_hours=0, scheduled_date=start_69)
    assert _run(db) == 0
    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        assert db2.get(Walk, "carlos").operational_status == "awaiting_payment"
    finally:
        db2.close()


def test_cutoff_respects_env_override(monkeypatch):
    """WALK_PAYMENT_CUTOFF_MINUTES configurável: com 90min, um início a 60min já
    entra no corte (antes de 45min não entraria)."""
    monkeypatch.setenv("WALK_PAYMENT_CUTOFF_MINUTES", "90")
    Factory, db = _setup()
    start_60 = (_local_now() + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M")
    _walk(db, "w60", "awaiting_payment", age_hours=1, scheduled_date=start_60)
    assert _run(db) == 1
    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        assert db2.get(Walk, "w60").operational_status == "ride_cancelled"
    finally:
        db2.close()
