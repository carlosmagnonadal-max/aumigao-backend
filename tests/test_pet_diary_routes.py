"""Testes do diário do tutor na timeline — POST/GET/DELETE (Perfil Vivo 2.0, Fase B).

O diário reusa PetTimelineEvent (Fase 1) com event_type="diary". O payload_json é
montado pelo servidor a partir de campos sanitizados (texto obrigatório <=2000,
humor opcional). Gate igual ao da timeline (Pro+).
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


def _ctx(active=True, plan="pro", with_trial=False):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    trial_ends = datetime.utcnow() + timedelta(days=10) if with_trial else None
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan, trial_ends_at=trial_ends))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    if active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))
    db.commit()
    return db


def _client(db, user, env, monkeypatch):
    if env:
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    else:
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()


def test_build_diary_entry_helper():
    """Unidade: montagem do payload/título no serviço (sem HTTP)."""
    from app.services.pet_profile_service import build_diary_entry

    # Sem título → deriva do texto.
    title, pj = build_diary_entry(text="  Rex correu.  ", mood="bom", title=None)
    assert title == "Rex correu."
    assert json.loads(pj) == {"text": "Rex correu.", "mood": "bom"}

    # Com título e sem humor.
    title, pj = build_diary_entry(text="ok", mood=None, title="  Passeio  ")
    assert title == "Passeio"
    data = json.loads(pj)
    assert data["title"] == "Passeio" and "mood" not in data

    # Texto longo → título truncado com reticências.
    long_text = "a" * 100
    title, pj = build_diary_entry(text=long_text, mood=None, title=None)
    assert title.endswith("…") and len(title) == 61


def test_diary_create_builds_payload(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary",
        "occurred_at": _now_iso(),
        "diary_text": "Rex brincou muito no parque hoje.",
        "diary_mood": "bom",
    })
    assert r.status_code == 201, r.text
    ev = r.json()["event"]
    assert ev["event_type"] == "diary"
    payload = json.loads(ev["payload_json"])
    assert payload["text"] == "Rex brincou muito no parque hoje."
    assert payload["mood"] == "bom"
    # Título derivado do texto quando não informado.
    assert ev["title"]


def test_diary_create_with_title(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary",
        "title": "Dia no parque",
        "occurred_at": _now_iso(),
        "diary_text": "Correu bastante.",
    })
    assert r.status_code == 201, r.text
    ev = r.json()["event"]
    assert ev["title"] == "Dia no parque"
    assert "mood" not in json.loads(ev["payload_json"])


def test_diary_text_required(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary",
        "occurred_at": _now_iso(),
    })
    assert r.status_code == 422


def test_diary_text_too_long_rejected(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary",
        "occurred_at": _now_iso(),
        "diary_text": "x" * 2001,
    })
    assert r.status_code == 422


def test_diary_invalid_mood_rejected(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary",
        "occurred_at": _now_iso(),
        "diary_text": "Ok",
        "diary_mood": "feliz",
    })
    assert r.status_code == 422


def test_non_diary_still_requires_title(monkeypatch):
    """Regressão: outros tipos continuam exigindo title."""
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "custom",
        "occurred_at": _now_iso(),
    })
    assert r.status_code == 422


def test_diary_appears_in_timeline_get(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    c.post("/api/pets/p1/timeline", json={
        "event_type": "diary", "occurred_at": _now_iso(),
        "diary_text": "Entrada de diário", "diary_mood": "neutro",
    })
    r = c.get("/api/pets/p1/timeline")
    assert r.status_code == 200
    events = r.json()["events"]
    diary = next(e for e in events if e["event_type"] == "diary")
    assert json.loads(diary["payload_json"])["mood"] == "neutro"


def test_diary_delete_by_owner(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    created = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary", "occurred_at": _now_iso(), "diary_text": "apagar",
    }).json()["event"]
    r = c.delete(f"/api/pets/p1/timeline/{created['id']}")
    assert r.status_code == 200
    assert db.query(PetTimelineEvent).count() == 0


def test_diary_free_plan_403(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=False)
    c = _client(db, db.get(User, "u1"), True, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary", "occurred_at": _now_iso(), "diary_text": "x",
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "plan_upgrade_required"


def test_diary_gate_off_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, db.get(User, "u1"), False, monkeypatch)
    r = c.post("/api/pets/p1/timeline", json={
        "event_type": "diary", "occurred_at": _now_iso(), "diary_text": "x",
    })
    assert r.status_code == 404
