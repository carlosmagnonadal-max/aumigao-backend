"""Testes de rota da Fase A — health-records, health-card, pet-briefing + gating."""
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
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.routes import pet_health as routes
from app.routes import pet_profile as profile_routes


def _ctx(active=True, plan="pro", with_trial=False):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    trial_ends = datetime.utcnow() + timedelta(days=10) if with_trial else None
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan, trial_ends_at=trial_ends))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="adm", email="adm@x.com", password_hash="x", role="admin", tenant_id="t1"))
    db.add(User(id="wk", email="wk@x.com", password_hash="x", role="walker", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex", breed="SRD", size="M"))
    db.add(Walk(id="w1", tutor_id="u1", tenant_id="t1", pet_id="p1", walker_id="wk",
                scheduled_date="2026-07-01", duration_minutes=30, price=0.0, status="Passeando agora"))
    if active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))
    db.commit()
    return db


def _client(db, user, env, monkeypatch, include_profile=False):
    if env:
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    else:
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(routes.api_router)
    app.include_router(routes.api_walk_router)
    if include_profile:
        app.include_router(profile_routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_dormant_returns_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, db.get(User, "u1"), env=False, monkeypatch=monkeypatch)
    assert c.get("/api/pets/p1/health-card").status_code == 404


def test_free_plan_returns_403_teaser(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=False)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/pets/p1/health-card")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "plan_upgrade_required"


def test_free_plan_trial_active_allows(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    assert c.get("/api/pets/p1/health-card").status_code == 200


# ---------------------------------------------------------------------------
# CRUD health-records
# ---------------------------------------------------------------------------

def test_create_list_delete_record(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    future = (date.today() + timedelta(days=200)).isoformat()
    r = c.post("/api/pets/p1/health-records", json={
        "kind": "vaccine", "name": "Antirrábica",
        "applied_at": date.today().isoformat(), "valid_until": future,
    })
    assert r.status_code == 201
    rid = r.json()["record"]["id"]
    assert r.json()["record"]["status"] == "em_dia"

    lst = c.get("/api/pets/p1/health-records")
    assert lst.status_code == 200 and len(lst.json()["records"]) == 1

    d = c.delete(f"/api/pets/p1/health-records/{rid}")
    assert d.status_code == 200
    assert len(c.get("/api/pets/p1/health-records").json()["records"]) == 0


def test_create_rejects_bad_kind(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/health-records", json={
        "kind": "xpto", "name": "X", "applied_at": date.today().isoformat()})
    assert r.status_code == 422


def test_valid_until_before_applied_rejected(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/health-records", json={
        "kind": "vaccine", "name": "X", "applied_at": date.today().isoformat(),
        "valid_until": (date.today() - timedelta(days=1)).isoformat()})
    assert r.status_code == 422


def test_other_users_pet_404(monkeypatch):
    db = _ctx(active=True)
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.commit()
    c = _client(db, db.get(User, "u2"), env=True, monkeypatch=monkeypatch)
    assert c.get("/api/pets/p1/health-card").status_code == 404


def test_admin_of_tenant_can_register(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "adm"), env=True, monkeypatch=monkeypatch)
    r = c.post("/api/pets/p1/health-records", json={
        "kind": "flea_tick", "name": "Bravecto", "applied_at": date.today().isoformat()})
    assert r.status_code == 201
    assert r.json()["record"]["created_by_role"] == "admin"


def test_admin_of_linked_tenant_can_access_health(monkeypatch):
    """0093 "pets seguem o tutor": admin cujo TENANT ATIVO (request.state.tenant_id)
    tem vínculo ATIVO com o dono do pet acessa a saúde, mesmo que o pet seja de OUTRO
    tenant de origem. Sem vínculo → 404."""
    from app.core.request_context import tenant_id_var
    from app.models.tenant_tutor_access import TenantTutorAccess

    db = _ctx(active=True)  # pet p1 nasce no tenant t1 (ativo + pro)
    # Tenant B com um admin próprio (nasceu em B).
    db.add(Tenant(id="tB", name="TB", slug="tb", status="active", plan="pro"))
    db.add(User(id="admB", email="admb@x.com", password_hash="x", role="admin", tenant_id="tB"))
    db.commit()
    admB = db.get(User, "admB")

    # Sem vínculo entre o tutor (u1) e o tenant B → 404, mesmo com tenant ativo = B.
    token = tenant_id_var.set("tB")
    try:
        c = _client(db, admB, env=True, monkeypatch=monkeypatch)
        assert c.get("/api/pets/p1/health-card").status_code == 404

        # Cria o vínculo ativo tutor↔tenant B → admin de B passa a acessar.
        db.add(TenantTutorAccess(id="lnk1", tenant_id="tB", tutor_user_id="u1", status="active"))
        db.commit()
        assert c.get("/api/pets/p1/health-card").status_code == 200

        # Vínculo revogado volta a bloquear.
        db.get(TenantTutorAccess, "lnk1").status = "revoked"
        db.commit()
        assert c.get("/api/pets/p1/health-card").status_code == 404
    finally:
        tenant_id_var.reset(token)


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------

def test_briefing_walker_access(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "wk"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/walks/w1/pet-briefing")
    assert r.status_code == 200
    assert r.json()["identity"]["name"] == "Rex"


def test_briefing_admin_access(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "adm"), env=True, monkeypatch=monkeypatch)
    assert c.get("/api/walks/w1/pet-briefing").status_code == 200


def test_briefing_stranger_403(monkeypatch):
    db = _ctx(active=True)
    db.add(User(id="wk2", email="wk2@x.com", password_hash="x", role="walker", tenant_id="t1"))
    db.commit()
    c = _client(db, db.get(User, "wk2"), env=True, monkeypatch=monkeypatch)
    assert c.get("/api/walks/w1/pet-briefing").status_code == 403


def test_briefing_dormant_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, db.get(User, "wk"), env=False, monkeypatch=monkeypatch)
    assert c.get("/api/walks/w1/pet-briefing").status_code == 404


def test_briefing_free_plan_403(monkeypatch):
    db = _ctx(active=True, plan="free", with_trial=False)
    c = _client(db, db.get(User, "wk"), env=True, monkeypatch=monkeypatch)
    r = c.get("/api/walks/w1/pet-briefing")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "plan_upgrade_required"


# ---------------------------------------------------------------------------
# PATCH profile — dieta + evento na timeline
# ---------------------------------------------------------------------------

def test_patch_diet_emits_timeline_event(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch, include_profile=True)
    r = c.patch("/api/pets/p1/profile", json={"diet_type": "seca", "diet_grams_per_meal": 120,
                                              "allergies": "frango"})
    assert r.status_code == 200
    db.expire_all()
    pet = db.get(Pet, "p1")
    assert pet.diet_type == "seca"
    assert pet.diet_grams_per_meal == 120
    events = db.query(PetTimelineEvent).filter(PetTimelineEvent.pet_id == "p1").all()
    assert len(events) == 1
    assert events[0].event_type == "health_note"
    # Payload sem valores sensíveis — só as chaves alteradas.
    import json
    changed = json.loads(events[0].payload_json)["changed_fields"]
    assert "diet_type" in changed and "allergies" in changed
    assert "seca" not in events[0].payload_json  # nenhum valor vazado


def test_patch_diet_type_invalid_422(monkeypatch):
    db = _ctx(active=True)
    c = _client(db, db.get(User, "u1"), env=True, monkeypatch=monkeypatch, include_profile=True)
    r = c.patch("/api/pets/p1/profile", json={"diet_type": "gourmet"})
    assert r.status_code == 422
