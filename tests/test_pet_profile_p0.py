"""Testes do Perfil Vivo P0 — registro rápido (timeline) + ficha expandida.

Cobre:
  - POST /pets/{id}/timeline para cada event_type de registro rápido (title default);
  - tipo inválido → 422;
  - title custom respeitado; occurred_at do cliente aceito;
  - GET devolve event_type + source;
  - PATCH /pets/{id}/profile com campos novos (supplements, vet_clinic, seguro,
    behavior_with_*, fear_triggers) + validação de enum de comportamento;
  - PUT /pets (CRUD) grava/lê os campos novos via PetBase.
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
from app.models.pet_timeline_event import QUICK_EVENT_TITLES, QUICK_EVENT_TYPES
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_diary_routes  # noqa: F401 — anexa rotas ao router
from app.routes import pet_profile as routes
from app.routes import pets as pets_routes


def _ctx(active=True, plan="pro"):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    if active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))
    db.commit()
    return db


def _client(db, user, monkeypatch, include_pets=False):
    monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    app = FastAPI()
    app.include_router(routes.api_router)
    app.include_router(routes.router)
    if include_pets:
        app.include_router(pets_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()


# ── Registro rápido (timeline) ──────────────────────────────────────────────

def test_quick_event_each_type_default_title(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    for et in sorted(QUICK_EVENT_TYPES):
        r = c.post("/api/pets/p1/timeline", json={"event_type": et, "occurred_at": _now_iso()})
        assert r.status_code == 201, f"{et}: {r.text}"
        ev = r.json()["event"]
        assert ev["event_type"] == et
        assert ev["title"] == QUICK_EVENT_TITLES[et]
        assert ev["source"] == "tutor"


def test_quick_event_custom_title_respected(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "meal", "title": "Café da manhã", "occurred_at": _now_iso(),
        "notes": "ração + patê",
    })
    assert r.status_code == 201, r.text
    ev = r.json()["event"]
    assert ev["title"] == "Café da manhã"
    assert ev["notes"] == "ração + patê"


def test_quick_event_client_timestamp_accepted(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    past = (datetime.utcnow() - timedelta(hours=3)).replace(microsecond=0)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "potty_pee", "occurred_at": past.isoformat(),
    })
    assert r.status_code == 201, r.text
    assert r.json()["event"]["occurred_at"].startswith(past.isoformat()[:16])


def test_invalid_event_type_422(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={"event_type": "teleport", "occurred_at": _now_iso()})
    assert r.status_code == 422


def test_quick_event_appears_in_get_with_source(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    c.post("/api/pets/p1/timeline", json={"event_type": "water", "occurred_at": _now_iso()})
    r = c.get("/api/pets/p1/timeline")
    assert r.status_code == 200
    ev = next(e for e in r.json()["events"] if e["event_type"] == "water")
    assert ev["source"] == "tutor" and ev["title"] == "Água"


# ── Ficha expandida (PATCH /profile) ────────────────────────────────────────

def test_patch_profile_new_fields(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    r = c.patch("/api/pets/p1/profile", json={
        "supplements_json": json.dumps([{"name": "Ômega 3", "dose": "1", "frequency": "diária"}]),
        "food_bag_weight_kg": 15.0,
        "food_bag_opened_at": "2026-07-01",
        "vet_clinic": "Clínica PetVida",
        "insurance_provider": "PetLove Saúde",
        "insurance_policy": "APX-123",
        "behavior_with_dogs": "amigavel",
        "behavior_with_children": "indiferente",
        "behavior_with_cats": "reativo",
        "fear_triggers_json": json.dumps(["trovão", "fogos"]),
    })
    assert r.status_code == 200, r.text
    pet = db.get(Pet, "p1")
    db.refresh(pet)
    assert pet.vet_clinic == "Clínica PetVida"
    assert pet.behavior_with_dogs == "amigavel"
    assert pet.food_bag_weight_kg == 15.0
    assert json.loads(pet.fear_triggers_json) == ["trovão", "fogos"]


def test_patch_profile_invalid_behavior_422(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    r = c.patch("/api/pets/p1/profile", json={"behavior_with_dogs": "feroz"})
    assert r.status_code == 422


def test_patch_profile_records_timeline_event(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch)
    c.patch("/api/pets/p1/profile", json={"vet_clinic": "PetVida"})
    r = c.get("/api/pets/p1/timeline")
    ev = next(e for e in r.json()["events"] if e["event_type"] == "health_note")
    assert "vet_clinic" in json.loads(ev["payload_json"])["changed_fields"]


# ── CRUD do pet (PetBase) ───────────────────────────────────────────────────

def test_pet_crud_new_fields_roundtrip(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), monkeypatch, include_pets=True)
    r = c.put("/pets/p1", json={
        "name": "Rex",
        "insurance_provider": "PetLove",
        "behavior_with_cats": "desconhecido",
        "food_bag_weight_kg": 3.0,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["insurance_provider"] == "PetLove"
    assert body["behavior_with_cats"] == "desconhecido"
    assert body["food_bag_weight_kg"] == 3.0
