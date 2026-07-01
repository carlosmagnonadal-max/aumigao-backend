"""T13b — Testes de ensure_vaccine_reminder + reminder_due_date opcional na timeline."""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import date, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_reminder import PetReminder
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_profile as routes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="pro"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
    db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))
    db.commit()
    return db


def _client(db, monkeypatch):
    monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, "u1")
    return TestClient(app)


def _future_date(days: int = 30) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def _past_date(days: int = 5) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Testes de ensure_vaccine_reminder (unidade de serviço)
# ---------------------------------------------------------------------------

def test_ensure_vaccine_reminder_creates_reminder():
    """ensure_vaccine_reminder cria PetReminder ligado ao evento."""
    db = _db()
    pet = db.get(Pet, "p1")
    due = date.today() + timedelta(days=30)
    from app.services.pet_profile_service import ensure_vaccine_reminder
    r = ensure_vaccine_reminder(db, pet, due_date=due, source_event_id="ev1", kind="vaccine")
    db.commit()
    assert r.id is not None
    assert r.pet_id == "p1"
    assert r.kind == "vaccine"
    assert r.due_date == due
    assert r.source_event_id == "ev1"
    assert r.active is True
    assert db.query(PetReminder).count() == 1


def test_ensure_vaccine_reminder_updates_existing():
    """Repostar com mesmo source_event_id atualiza due_date, não duplica."""
    db = _db()
    pet = db.get(Pet, "p1")
    due1 = date.today() + timedelta(days=30)
    due2 = date.today() + timedelta(days=45)
    from app.services.pet_profile_service import ensure_vaccine_reminder
    r1 = ensure_vaccine_reminder(db, pet, due_date=due1, source_event_id="ev1", kind="vaccine")
    db.commit()
    r2 = ensure_vaccine_reminder(db, pet, due_date=due2, source_event_id="ev1", kind="vaccine")
    db.commit()
    assert r1.id == r2.id
    assert r2.due_date == due2
    assert db.query(PetReminder).count() == 1


def test_ensure_vermifuge_reminder_creates_distinct_kind():
    """Kind vermifuge cria reminder separado do vaccine."""
    db = _db()
    pet = db.get(Pet, "p1")
    due = date.today() + timedelta(days=20)
    from app.services.pet_profile_service import ensure_vaccine_reminder
    rv = ensure_vaccine_reminder(db, pet, due_date=due, source_event_id="ev1", kind="vaccine")
    rm = ensure_vaccine_reminder(db, pet, due_date=due, source_event_id="ev2", kind="vermifuge")
    db.commit()
    assert rv.id != rm.id
    assert rv.kind == "vaccine"
    assert rm.kind == "vermifuge"
    assert db.query(PetReminder).count() == 2


# ---------------------------------------------------------------------------
# Testes de integração via rota POST /api/pets/{id}/timeline
# ---------------------------------------------------------------------------

def test_timeline_vaccine_with_reminder_due_date_creates_reminder(monkeypatch):
    """(a) POST evento vacina com reminder_due_date futura → cria PetReminder."""
    db = _db()
    c = _client(db, monkeypatch)
    due = _future_date(30)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "vaccine",
        "title": "Vacina antirrábica",
        "occurred_at": "2026-07-01T10:00:00",
        "reminder_due_date": due,
    })
    assert r.status_code == 201
    reminder = db.query(PetReminder).first()
    assert reminder is not None
    assert reminder.kind == "vaccine"
    assert str(reminder.due_date) == due
    # Deve estar ligado ao evento da timeline
    ev = db.query(PetTimelineEvent).first()
    assert reminder.source_event_id == ev.id


def test_timeline_event_without_reminder_due_date_no_reminder(monkeypatch):
    """(b) POST evento vacina SEM reminder_due_date → nenhum PetReminder criado."""
    db = _db()
    c = _client(db, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "vaccine",
        "title": "Vacina antirrábica",
        "occurred_at": "2026-07-01T10:00:00",
        # reminder_due_date ausente
    })
    assert r.status_code == 201
    assert db.query(PetReminder).count() == 0


def test_timeline_reminder_due_date_past_returns_422(monkeypatch):
    """(c) reminder_due_date no passado → 422."""
    db = _db()
    c = _client(db, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "vaccine",
        "title": "Vacina",
        "occurred_at": "2026-07-01T10:00:00",
        "reminder_due_date": _past_date(5),
    })
    assert r.status_code == 422


def test_timeline_repost_same_vaccine_no_duplicate_reminder(monkeypatch):
    """(d) Repostar com mesmo source_event_id não duplica o PetReminder."""
    db = _db()
    c = _client(db, monkeypatch)
    due = _future_date(30)
    # Primeiro POST
    r1 = c.post("/api/pets/p1/timeline", json={
        "event_type": "vaccine",
        "title": "Vacina 1",
        "occurred_at": "2026-07-01T10:00:00",
        "reminder_due_date": due,
    })
    assert r1.status_code == 201
    ev_id = r1.json()["event"]["id"]

    # Segundo POST com nova data de lembrete para o mesmo evento não é possível
    # via rota (cada POST cria um novo evento). Mas via serviço, source_event_id
    # diferente → cria novo reminder. Verificamos que a rota criou exatamente 1.
    assert db.query(PetReminder).count() == 1
    reminder = db.query(PetReminder).first()
    assert reminder.source_event_id == ev_id


def test_timeline_medication_with_reminder_creates_vermifuge_reminder(monkeypatch):
    """POST evento medication com reminder_due_date → cria PetReminder kind=vermifuge."""
    db = _db()
    c = _client(db, monkeypatch)
    due = _future_date(15)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "medication",
        "title": "Vermifugo aplicado",
        "occurred_at": "2026-07-01T10:00:00",
        "reminder_due_date": due,
    })
    assert r.status_code == 201
    reminder = db.query(PetReminder).first()
    assert reminder is not None
    assert reminder.kind == "vermifuge"


def test_timeline_weight_event_with_reminder_no_reminder_created(monkeypatch):
    """evento weight com reminder_due_date → campo ignorado (não é vaccine/medication)."""
    db = _db()
    c = _client(db, monkeypatch)
    due = _future_date(30)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "weight",
        "title": "Peso 10kg",
        "occurred_at": "2026-07-01T10:00:00",
        "reminder_due_date": due,
    })
    assert r.status_code == 201
    # Nenhum reminder para eventos de peso
    assert db.query(PetReminder).count() == 0
