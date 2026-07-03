"""Mapa de convivência do pet a partir dos shared walks (Perfil Vivo 2.0, Fase E).

GET /api/pets/{pet_id}/companions: pets que dividiram shared walk concluído com este.
Sanitizado: só nome/foto/raça do outro pet — NUNCA tutor/endereço/contato.
Mesmo tenant apenas. Gate Pro+. Sem shared walks = lista vazia.
"""
from __future__ import annotations

import app.models  # noqa: F401

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
from app.models.shared_walk import (
    PARTICIPANT_CANCELLED,
    PARTICIPANT_PAID,
    SHARED_CANCELLED,
    SHARED_CONFIRMED,
    SharedWalk,
    SharedWalkParticipant,
)
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.routes import pet_behavior_routes  # noqa: F401 — anexa rotas Fase E
from app.routes import pet_profile as routes


def _ctx(plan="pro", active=True):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan=plan))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="u3", email="u3@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex", breed="Vira-lata", photo_url="http://x/rex.jpg"))
    db.add(Pet(id="p2", tutor_id="u2", tenant_id="t1", name="Bud", breed="Poodle", photo_url="http://x/bud.jpg"))
    db.add(Pet(id="p3", tutor_id="u3", tenant_id="t1", name="Mel", breed="Labrador"))
    if active:
        db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True))
        db.add(PetProfileConfig(tenant_id="t1", profile_enabled=True))
    db.commit()
    return db


def _client(db, user_id, env, monkeypatch):
    if env:
        monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    else:
        monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
    app = FastAPI()
    app.include_router(routes.api_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return TestClient(app)


def _shared_walk(db, sw_id, tenant_id, status, participants, when=None):
    """participants: list of (tutor_id, pet_id, part_status)."""
    when = when or datetime.utcnow()
    db.add(SharedWalk(id=sw_id, tenant_id=tenant_id, created_by_tutor_id=participants[0][0],
                      status=status, created_at=when, confirmed_at=when))
    for i, (tid, pid, pstatus) in enumerate(participants):
        db.add(SharedWalkParticipant(
            id=f"{sw_id}-{i}", shared_walk_id=sw_id, tutor_id=tid, pet_id=pid,
            status=pstatus, tenant_id=tenant_id, created_at=when,
        ))
    db.commit()


def test_empty_when_no_shared_walks(monkeypatch):
    db = _ctx()
    c = _client(db, "u1", True, monkeypatch)
    r = c.get("/api/pets/p1/companions")
    assert r.status_code == 200
    assert r.json() == {"pet_id": "p1", "companions": [], "total": 0}


def test_single_companion(monkeypatch):
    db = _ctx()
    _shared_walk(db, "sw1", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID),
        ("u2", "p2", PARTICIPANT_PAID),
    ])
    c = _client(db, "u1", True, monkeypatch)
    r = c.get("/api/pets/p1/companions")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 1
    comp = data["companions"][0]
    assert comp["pet_id"] == "p2"
    assert comp["name"] == "Bud"
    assert comp["breed"] == "Poodle"
    assert comp["photo_url"] == "http://x/bud.jpg"
    assert comp["walks_together"] == 1
    assert comp["last_walk_at"] is not None


def test_sanitization_no_tutor_or_contact(monkeypatch):
    db = _ctx()
    _shared_walk(db, "sw1", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID),
        ("u2", "p2", PARTICIPANT_PAID),
    ])
    c = _client(db, "u1", True, monkeypatch)
    comp = c.get("/api/pets/p1/companions").json()["companions"][0]
    # Apenas campos sanitizados; NUNCA tutor/endereço/contato.
    assert set(comp.keys()) == {"pet_id", "name", "photo_url", "breed", "walks_together", "last_walk_at"}
    assert "tutor_id" not in comp
    assert "u2" not in str(comp)


def test_walks_together_counts_and_orders(monkeypatch):
    db = _ctx()
    now = datetime.utcnow()
    # p2 dividiu 2 shared walks com p1; p3 dividiu 1.
    _shared_walk(db, "sw1", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID), ("u2", "p2", PARTICIPANT_PAID),
    ], when=now - timedelta(days=3))
    _shared_walk(db, "sw2", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID), ("u2", "p2", PARTICIPANT_PAID), ("u3", "p3", PARTICIPANT_PAID),
    ], when=now - timedelta(days=1))
    c = _client(db, "u1", True, monkeypatch)
    comps = c.get("/api/pets/p1/companions").json()["companions"]
    assert [x["pet_id"] for x in comps] == ["p2", "p3"]  # p2 (2) antes de p3 (1)
    assert comps[0]["walks_together"] == 2
    assert comps[1]["walks_together"] == 1


def test_cancelled_walk_excluded(monkeypatch):
    db = _ctx()
    _shared_walk(db, "sw1", "t1", SHARED_CANCELLED, [
        ("u1", "p1", PARTICIPANT_CANCELLED), ("u2", "p2", PARTICIPANT_CANCELLED),
    ])
    c = _client(db, "u1", True, monkeypatch)
    r = c.get("/api/pets/p1/companions")
    assert r.json()["total"] == 0


def test_declined_participant_excluded(monkeypatch):
    """Companheiro que não pagou (cancelou participação) não conta."""
    db = _ctx()
    _shared_walk(db, "sw1", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID), ("u2", "p2", PARTICIPANT_CANCELLED),
    ])
    c = _client(db, "u1", True, monkeypatch)
    assert c.get("/api/pets/p1/companions").json()["total"] == 0


def test_self_not_listed_as_companion(monkeypatch):
    db = _ctx()
    # dois pets do MESMO tutor no mesmo shared walk — p1 não é companheiro de si.
    db.add(Pet(id="p1b", tutor_id="u1", tenant_id="t1", name="Rex2", breed="X"))
    db.commit()
    _shared_walk(db, "sw1", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID), ("u1", "p1b", PARTICIPANT_PAID),
    ])
    c = _client(db, "u1", True, monkeypatch)
    comps = c.get("/api/pets/p1/companions").json()["companions"]
    assert [x["pet_id"] for x in comps] == ["p1b"]  # o outro pet do tutor conta; p1 nunca


def test_cross_tenant_isolation(monkeypatch):
    db = _ctx()
    # shared walk de OUTRO tenant não vaza (mesmo pet_id não existe, mas garante o filtro)
    db.add(Tenant(id="t2", name="T2", slug="t2", status="active", plan="pro"))
    db.add(User(id="u9", email="u9@x.com", password_hash="x", role="tutor", tenant_id="t2"))
    db.add(Pet(id="p9", tutor_id="u9", tenant_id="t2", name="Zeus", breed="X"))
    db.commit()
    # p1(t1) e p9(t2) — cenário artificial: shared walk marcado t2 com participante p1
    _shared_walk(db, "sw1", "t2", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID), ("u9", "p9", PARTICIPANT_PAID),
    ])
    c = _client(db, "u1", True, monkeypatch)
    # p1 é do t1; o shared walk é do t2 → não deve retornar companheiro
    assert c.get("/api/pets/p1/companions").json()["total"] == 0


def test_admin_can_query(monkeypatch):
    db = _ctx()
    db.add(User(id="admin1", email="a@x.com", password_hash="x", role="admin", tenant_id="t1"))
    db.commit()
    _shared_walk(db, "sw1", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID), ("u2", "p2", PARTICIPANT_PAID),
    ])
    c = _client(db, "admin1", True, monkeypatch)
    r = c.get("/api/pets/p1/companions")
    assert r.status_code == 200
    assert r.json()["total"] == 1


def test_non_owner_tutor_forbidden(monkeypatch):
    db = _ctx()
    _shared_walk(db, "sw1", "t1", SHARED_CONFIRMED, [
        ("u1", "p1", PARTICIPANT_PAID), ("u2", "p2", PARTICIPANT_PAID),
    ])
    # u2 (dono do p2) tenta ver companheiros do p1 (que é do u1) → 404
    c = _client(db, "u2", True, monkeypatch)
    r = c.get("/api/pets/p1/companions")
    assert r.status_code == 404


def test_free_plan_403(monkeypatch):
    db = _ctx(plan="free")
    c = _client(db, "u1", True, monkeypatch)
    r = c.get("/api/pets/p1/companions")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "plan_upgrade_required"


def test_gate_off_404(monkeypatch):
    db = _ctx(active=False)
    c = _client(db, "u1", False, monkeypatch)
    r = c.get("/api/pets/p1/companions")
    assert r.status_code == 404
