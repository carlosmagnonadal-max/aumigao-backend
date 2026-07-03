"""Observação estruturada do TENANT na timeline (Perfil Vivo 2.0, Fase E).

O ADMIN do tenant registra avaliação/observação do pet: event_type="tenant_note"
com payload montado NO SERVIDOR (padrão diary) — {context, category, text, title?}.
source="admin". Incidente/restrição notificam o TUTOR dono (best-effort).
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
from app.models.notification import Notification
from app.models.pet import Pet
from app.models.pet_profile_config import PetProfileConfig
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_behavior_routes  # noqa: F401 — anexa rotas Fase E
from app.routes import pet_profile as routes


def _ctx(active=True, plan="pro", role="super_admin"):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan))
    db.add(User(id="admin1", email="admin@x.com", password_hash="x", role=role, tenant_id="t1"))
    db.add(User(id="tutor1", email="tutor@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="tutor1", tenant_id="t1", name="Rex"))
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
    app.include_router(routes.api_admin_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_tenant_note_created_with_server_payload(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche",
        "category": "evolucao",
        "text": "Rex se socializou muito bem com a turma hoje.",
        "title": "Ótimo dia na creche",
    })
    assert r.status_code == 201, r.text
    ev = r.json()["event"]
    assert ev["event_type"] == "tenant_note"
    assert ev["source"] == "admin"
    assert ev["title"] == "Ótimo dia na creche"
    payload = json.loads(ev["payload_json"])
    assert payload["context"] == "creche"
    assert payload["category"] == "evolucao"
    assert payload["text"] == "Rex se socializou muito bem com a turma hoje."


def test_tenant_note_title_derived_from_text(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    long_text = "a" * 100
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "banho_tosa", "category": "cuidado", "text": long_text,
    })
    assert r.status_code == 201, r.text
    ev = r.json()["event"]
    assert ev["title"].endswith("…")


def test_tenant_note_ignores_client_payload_json(monkeypatch):
    """Payload é montado no servidor; payload cru do cliente é ignorado."""
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "outro", "category": "convivencia", "text": "ok",
        "payload_json": "{\"malicioso\": true}",
    })
    assert r.status_code == 201, r.text
    payload = json.loads(r.json()["event"]["payload_json"])
    assert "malicioso" not in payload
    assert payload["category"] == "convivencia"


def test_tenant_note_text_required(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "evolucao",
    })
    assert r.status_code == 422


def test_tenant_note_text_too_long_rejected(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "evolucao", "text": "x" * 2001,
    })
    assert r.status_code == 422


def test_tenant_note_invalid_context_rejected(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "inexistente", "category": "evolucao", "text": "ok",
    })
    assert r.status_code == 422


def test_tenant_note_invalid_category_rejected(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "inexistente", "text": "ok",
    })
    assert r.status_code == 422


def test_tenant_note_incident_notifies_owner(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "incidente",
        "text": "Rex teve um pequeno incidente com outro cão.",
    })
    assert r.status_code == 201, r.text
    notifs = db.query(Notification).filter(Notification.user_id == "tutor1").all()
    assert len(notifs) == 1
    assert notifs[0].user_role == "tutor"
    assert notifs[0].related_entity_type == "pet"
    assert notifs[0].related_entity_id == "p1"


def test_tenant_note_restricao_notifies_owner(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "adestramento", "category": "restricao",
        "text": "Não oferecer petiscos com corante — reação alérgica.",
    })
    assert r.status_code == 201, r.text
    assert db.query(Notification).filter(Notification.user_id == "tutor1").count() == 1


def test_tenant_note_non_incident_no_notification(monkeypatch):
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "evolucao", "text": "Tudo ótimo.",
    })
    assert db.query(Notification).count() == 0


def test_tenant_note_pet_other_tenant_404(monkeypatch):
    db = _ctx()
    # pet de outro tenant não é acessível ao admin escopado no t1
    db.add(Tenant(id="t2", name="T2", slug="t2", status="active", plan="pro"))
    db.add(User(id="tutor2", email="t2@x.com", password_hash="x", role="tutor", tenant_id="t2"))
    db.add(Pet(id="p2", tutor_id="tutor2", tenant_id="t2", name="Bud"))
    db.commit()
    # super_admin operando como tenant t1 (act-as) → escopo restrito ao t1.
    admin = db.get(User, "admin1")
    admin._act_as_tenant_id = "t1"
    c = _client(db, admin, True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p2/timeline", json={
        "context": "creche", "category": "evolucao", "text": "ok",
    })
    assert r.status_code == 404


def test_tenant_note_free_plan_403(monkeypatch):
    db = _ctx(active=True, plan="free")
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "evolucao", "text": "x",
    })
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "plan_upgrade_required"


def test_tenant_note_gate_off_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, db.get(User, "admin1"), False, monkeypatch)
    r = c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "evolucao", "text": "x",
    })
    assert r.status_code == 404


def test_tenant_note_appears_in_tutor_timeline(monkeypatch):
    """O evento admin aparece na timeline do tutor com source=admin."""
    db = _ctx()
    c = _client(db, db.get(User, "admin1"), True, monkeypatch)
    c.post("/api/admin/pet-profile/pets/p1/timeline", json={
        "context": "creche", "category": "aprendizado", "text": "Aprendeu 'senta'.",
    })
    ev = db.query(PetTimelineEvent).filter(PetTimelineEvent.event_type == "tenant_note").first()
    assert ev is not None
    assert ev.source == "admin"
    assert ev.pet_id == "p1"
    assert ev.tenant_id == "t1"
