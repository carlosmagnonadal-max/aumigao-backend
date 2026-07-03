"""Filtro por categoria na timeline do pet (Perfil Vivo 2.0, Fase E).

GET timeline ganha ?category= opcional. Filtra tenant_note pela category do payload
e mapeia tipos existentes em categorias default. Sem param = comportamento intacto.
"""
from __future__ import annotations

import app.models  # noqa: F401

import json
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_profile as routes
from app.services.pet_profile_service import record_timeline_event


def _ctx(plan="pro"):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan))
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


def _seed_events(db):
    pet = db.get(Pet, "p1")
    base = datetime.utcnow()
    # walk_observation -> convivencia
    record_timeline_event(db, pet, event_type="walk_observation", title="Obs",
                          occurred_at=base - timedelta(minutes=1), source="walker")
    # self_walk -> convivencia
    record_timeline_event(db, pet, event_type="self_walk", title="Self",
                          occurred_at=base - timedelta(minutes=2), source="tutor")
    # health_note -> cuidado
    record_timeline_event(db, pet, event_type="health_note", title="Saúde",
                          occurred_at=base - timedelta(minutes=3), source="tutor")
    # vaccine -> cuidado
    record_timeline_event(db, pet, event_type="vaccine", title="Vacina",
                          occurred_at=base - timedelta(minutes=4), source="tutor")
    # tenant_note incidente
    record_timeline_event(db, pet, event_type="tenant_note", title="Incidente",
                          occurred_at=base - timedelta(minutes=5), source="admin",
                          payload_json=json.dumps({"context": "creche", "category": "incidente", "text": "x"}))
    # tenant_note evolucao
    record_timeline_event(db, pet, event_type="tenant_note", title="Evolução",
                          occurred_at=base - timedelta(minutes=6), source="admin",
                          payload_json=json.dumps({"context": "creche", "category": "evolucao", "text": "y"}))
    # diary -> sem categoria (aparece em todas)
    record_timeline_event(db, pet, event_type="diary", title="Diário",
                          occurred_at=base - timedelta(minutes=7), source="tutor",
                          payload_json=json.dumps({"text": "z"}))
    db.commit()


def test_no_param_returns_all_intact(monkeypatch):
    db = _ctx()
    _seed_events(db)
    c = _client(db, monkeypatch)
    r = c.get("/api/pets/p1/timeline")
    assert r.status_code == 200
    assert len(r.json()["events"]) == 7


def test_filter_convivencia(monkeypatch):
    db = _ctx()
    _seed_events(db)
    c = _client(db, monkeypatch)
    r = c.get("/api/pets/p1/timeline?category=convivencia")
    assert r.status_code == 200
    types = sorted(e["event_type"] for e in r.json()["events"])
    # walk_observation + self_walk (+ diary aparece em todas)
    assert "walk_observation" in types
    assert "self_walk" in types
    assert "diary" in types
    assert "health_note" not in types
    assert "vaccine" not in types


def test_filter_cuidado(monkeypatch):
    db = _ctx()
    _seed_events(db)
    c = _client(db, monkeypatch)
    r = c.get("/api/pets/p1/timeline?category=cuidado")
    assert r.status_code == 200
    types = [e["event_type"] for e in r.json()["events"]]
    assert "health_note" in types
    assert "vaccine" in types
    assert "diary" in types  # sem categoria = aparece em todas
    assert "walk_observation" not in types


def test_filter_incidente_only_tenant_note(monkeypatch):
    db = _ctx()
    _seed_events(db)
    c = _client(db, monkeypatch)
    r = c.get("/api/pets/p1/timeline?category=incidente")
    assert r.status_code == 200
    events = r.json()["events"]
    tenant_notes = [e for e in events if e["event_type"] == "tenant_note"]
    assert len(tenant_notes) == 1
    assert json.loads(tenant_notes[0]["payload_json"])["category"] == "incidente"
    # o tenant_note de evolucao NÃO aparece
    assert all(json.loads(e["payload_json"]).get("category") != "evolucao"
               for e in tenant_notes)


def test_filter_evolucao_matches_tenant_note(monkeypatch):
    db = _ctx()
    _seed_events(db)
    c = _client(db, monkeypatch)
    r = c.get("/api/pets/p1/timeline?category=evolucao")
    assert r.status_code == 200
    tenant_notes = [e for e in r.json()["events"] if e["event_type"] == "tenant_note"]
    assert len(tenant_notes) == 1
    assert json.loads(tenant_notes[0]["payload_json"])["category"] == "evolucao"


def test_invalid_category_rejected(monkeypatch):
    db = _ctx()
    _seed_events(db)
    c = _client(db, monkeypatch)
    r = c.get("/api/pets/p1/timeline?category=inexistente")
    assert r.status_code == 422


def test_diary_appears_in_every_category(monkeypatch):
    db = _ctx()
    _seed_events(db)
    c = _client(db, monkeypatch)
    for cat in ("evolucao", "aprendizado", "cuidado", "convivencia", "incidente", "restricao"):
        r = c.get(f"/api/pets/p1/timeline?category={cat}")
        assert r.status_code == 200, cat
        types = [e["event_type"] for e in r.json()["events"]]
        assert "diary" in types, f"diary deve aparecer em {cat}"
