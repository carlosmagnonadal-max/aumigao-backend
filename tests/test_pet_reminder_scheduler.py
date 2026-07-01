"""T13 — Testes da task _task_pet_reminder_alerts no scheduler (Fase 3).

Nota de arquitetura dos testes: record_operational_log chama inspect(engine).has_table()
que no SQLite + StaticPool causa rollback implícito da conexão, descartando rows
flushed-but-uncommitted. Para evitar falsos negativos, _run_task() faz commit antes de
retornar. Verificações de state post-run usam uma sessão nova (db2) para contornar o
state da sessão original que pode estar corrompido pelo rollback do inspect.
"""
from __future__ import annotations

import app.models  # noqa: F401 — garante todos os mappers

from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.notification import Notification
from app.models.operational_beta_log import OperationalBetaLog
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_reminder import PetReminder
from app.models.tenant import Tenant, TenantFeature
from app.models.walk import Walk


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _engine_and_session():
    """Retorna (engine, Session factory) para um banco SQLite em memória."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Factory = sessionmaker(bind=eng)
    db = Factory()
    db.info["rls_tenant"] = "*"
    return eng, Factory, db


def _enable_reminders(db, tenant_id: str = "t1") -> PetProfileConfig:
    cfg = db.query(PetProfileConfig).filter_by(tenant_id=tenant_id).first()
    if not cfg:
        cfg = PetProfileConfig(tenant_id=tenant_id, reminders_enabled=True)
        db.add(cfg)
    else:
        cfg.reminders_enabled = True
    db.flush()
    feat = db.query(TenantFeature).filter_by(tenant_id=tenant_id, feature_key="pet_alerts").first()
    if not feat:
        db.add(TenantFeature(tenant_id=tenant_id, feature_key="pet_alerts", enabled=True))
    else:
        feat.enabled = True
    db.commit()
    return cfg


def _make_tenant(db, tid: str = "t1") -> Tenant:
    t = db.get(Tenant, tid)
    if not t:
        t = Tenant(id=tid, name=f"Tenant {tid}", slug=tid, status="active", plan="pro")
        db.add(t)
        db.commit()
    return t


def _make_pet(db, pid: str = "p1", tutor_id: str = "u1", tenant_id: str = "t1",
              birth_date: date | None = None) -> Pet:
    p = Pet(id=pid, tutor_id=tutor_id, tenant_id=tenant_id, name=f"Pet {pid}",
            birth_date=birth_date)
    db.add(p)
    db.commit()
    return p


def _make_completed_walk(db, wid: str, pet_id: str, tutor_id: str = "u1",
                          tenant_id: str = "t1", when: datetime | None = None) -> Walk:
    when = when or datetime.utcnow()
    w = Walk(
        id=wid, tutor_id=tutor_id, pet_id=pet_id, tenant_id=tenant_id,
        walker_id="walker1",
        scheduled_date=when.date().isoformat(),
        duration_minutes=30, price=50.0,
        status="Finalizado",
        operational_status="ride_completed",
        created_at=when,
    )
    db.add(w)
    db.commit()
    return w


def _run_task(db, Factory) -> int:
    """Roda a task e retorna count.

    A task pet_reminder_alerts faz commit interno antes de chamar record_operational_log
    (que usa inspect(engine).has_table() — causa rollback implícito no SQLite+StaticPool).
    O scheduler real também comita via _run_task wrapper após a task retornar.
    Aqui o commit já ocorreu internamente; o db.commit() final é no-op mas garantido.
    """
    from app.services.pet_reminder_service import task_pet_reminder_alerts
    count = task_pet_reminder_alerts(db)
    # O commit já foi feito internamente; este garante o log de sweep também.
    try:
        db.commit()
    except Exception:
        pass
    return count


def _count_notifications(Factory) -> int:
    """Abre uma sessão nova para contar notificações (evita state corrompido do inspect)."""
    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        return db2.query(Notification).count()
    finally:
        db2.close()


def _get_notifications(Factory) -> list:
    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    try:
        return db2.query(Notification).all()
    finally:
        db2.close()


# ---------------------------------------------------------------------------
# (a) env PET_ALERTS_ENABLED off → 0 e nada criado
# ---------------------------------------------------------------------------

def test_env_off_returns_zero_and_no_reminders(monkeypatch):
    monkeypatch.delenv("PET_ALERTS_ENABLED", raising=False)
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    _enable_reminders(db)
    _make_pet(db, "p1", birth_date=date.today())

    result = _run_task(db, Factory)

    assert result == 0
    assert _count_notifications(Factory) == 0
    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    assert db2.query(PetReminder).count() == 0
    db2.close()


# ---------------------------------------------------------------------------
# (b) guard diário: log recente → 0
# ---------------------------------------------------------------------------

def test_guard_daily_returns_zero_when_log_recent(monkeypatch):
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    _enable_reminders(db)

    # Injeta log recente (5 min atrás)
    db.add(OperationalBetaLog(
        id=str(uuid4()),
        event_type="pet_reminder_sweep",
        source="scheduler.reminders",
        severity="info",
        message="sweep anterior",
        context_json="{}",
        created_at=datetime.utcnow() - timedelta(minutes=5),
    ))
    db.commit()

    result = _run_task(db, Factory)
    assert result == 0


# ---------------------------------------------------------------------------
# (c) vacina dentro da janela → 1 notificação + idempotência 7d
# ---------------------------------------------------------------------------

def test_vaccine_reminder_notifies_and_idempotent(monkeypatch):
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    _enable_reminders(db)
    _make_pet(db, "p1")

    today = date.today()
    # Vacina vence em 10 dias (dentro da janela padrão de 15 dias)
    due = today + timedelta(days=10)
    reminder = PetReminder(pet_id="p1", tenant_id="t1", kind="vaccine",
                           due_date=due, active=True)
    db.add(reminder)
    db.commit()

    count1 = _run_task(db, Factory)

    assert count1 == 1
    notifs = _get_notifications(Factory)
    assert len(notifs) == 1
    assert notifs[0].type == "pet_reminder"

    # last_notified_at deve estar setado — busca em sessão nova
    db2 = Factory()
    db2.info["rls_tenant"] = "*"
    r = db2.query(PetReminder).filter_by(kind="vaccine").first()
    assert r is not None and r.last_notified_at is not None
    db2.close()

    # Segunda execução: last_notified_at < 7d → 0 novas notificações.
    # Remove guard de sweep para não bloquear via guard diário.
    db3 = Factory()
    db3.info["rls_tenant"] = "*"
    db3.query(OperationalBetaLog).filter_by(event_type="pet_reminder_sweep").delete()
    db3.commit()
    count2 = _run_task(db3, Factory)
    assert count2 == 0
    assert _count_notifications(Factory) == 1
    db3.close()


# ---------------------------------------------------------------------------
# (d) vacina fora da janela (due muito no futuro) → 0
# ---------------------------------------------------------------------------

def test_vaccine_outside_window_no_notification(monkeypatch):
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    _enable_reminders(db)
    _make_pet(db, "p1")

    today = date.today()
    due = today + timedelta(days=60)  # além da janela de 15 dias
    reminder = PetReminder(pet_id="p1", tenant_id="t1", kind="vaccine",
                           due_date=due, active=True)
    db.add(reminder)
    db.commit()

    count = _run_task(db, Factory)
    assert count == 0
    assert _count_notifications(Factory) == 0


# ---------------------------------------------------------------------------
# (e) aniversário hoje → notifica 1x, 2º run no mesmo dia → 0
# ---------------------------------------------------------------------------

def test_birthday_notifies_once_per_day(monkeypatch):
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    _enable_reminders(db)
    today = date.today()
    _make_pet(db, "p1", birth_date=date(today.year - 3, today.month, today.day))

    count1 = _run_task(db, Factory)

    assert count1 == 1
    notifs = _get_notifications(Factory)
    assert len(notifs) == 1
    title_lower = notifs[0].title.lower()
    assert "aniversário" in title_lower or "aniversario" in title_lower

    # Segundo run: reminder já notificado este ano → 0.
    db3 = Factory()
    db3.info["rls_tenant"] = "*"
    db3.query(OperationalBetaLog).filter_by(event_type="pet_reminder_sweep").delete()
    db3.commit()
    count2 = _run_task(db3, Factory)
    assert count2 == 0
    assert _count_notifications(Factory) == 1
    db3.close()


# ---------------------------------------------------------------------------
# (f) inatividade: pet com walk concluído velho → notifica; sem walk → NÃO
# ---------------------------------------------------------------------------

def test_inactivity_notifies_only_pets_with_old_walk(monkeypatch):
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    _enable_reminders(db)
    # Pet com passeio concluído 20 dias atrás (acima do inactivity_days=10)
    _make_pet(db, "p1")
    old_walk_time = datetime.utcnow() - timedelta(days=20)
    _make_completed_walk(db, "w1", "p1", when=old_walk_time)

    # Pet sem passeio nenhum → NÃO deve alertar
    _make_pet(db, "p2", tutor_id="u2")

    count = _run_task(db, Factory)

    assert count == 1  # Apenas p1
    notifs = _get_notifications(Factory)
    assert len(notifs) == 1
    # A notificação é para o tutor do p1
    assert notifs[0].user_id == "u1"
    assert notifs[0].type == "pet_reminder"


def test_inactivity_recent_walk_no_notification(monkeypatch):
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    _enable_reminders(db)
    _make_pet(db, "p1")
    # Walk concluído recentemente (3 dias, abaixo do limite de 10)
    _make_completed_walk(db, "w1", "p1", when=datetime.utcnow() - timedelta(days=3))

    count = _run_task(db, Factory)
    assert count == 0
    assert _count_notifications(Factory) == 0


# ---------------------------------------------------------------------------
# (g) tenant com feature OFF → 0
# ---------------------------------------------------------------------------

def test_tenant_feature_off_returns_zero(monkeypatch):
    monkeypatch.setenv("PET_ALERTS_ENABLED", "true")
    eng, Factory, db = _engine_and_session()
    _make_tenant(db)
    # NÃO liga a feature (sem TenantFeature nem config.reminders_enabled)
    _make_pet(db, "p1", birth_date=date.today())

    count = _run_task(db, Factory)
    assert count == 0
    assert _count_notifications(Factory) == 0
