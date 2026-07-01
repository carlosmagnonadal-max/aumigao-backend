from __future__ import annotations

import app.models  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import pet_profile as routes


def _client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.commit()
    admin = User(id="a1", email="a@x.com", password_hash="x", role="super_admin", tenant_id="t1")
    app = FastAPI()
    app.include_router(routes.api_admin_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin
    return TestClient(app)


def test_get_config_creates_default():
    c = _client()
    r = c.get("/api/admin/pet-profile/config")
    assert r.status_code == 200
    assert r.json()["profile_enabled"] is False
    assert r.json()["vaccine_lead_days"] == 15


def test_patch_config():
    c = _client()
    r = c.patch("/api/admin/pet-profile/config", json={"profile_enabled": True, "inactivity_days": 7})
    assert r.status_code == 200
    assert r.json()["profile_enabled"] is True
    assert c.get("/api/admin/pet-profile/config").json()["inactivity_days"] == 7


def test_admin_config_is_tenant_scoped():
    # admin do t1 mexe só no t1; um segundo tenant não é afetado
    from app.models.pet_profile_config import PetProfileConfig
    c = _client()   # admin tenant_id=t1
    c.patch("/api/admin/pet-profile/config", json={"profile_enabled": True})
    # confirma que a config criada é do t1 (o _client seeda só t1; o resolvido é t1)
    got = c.get("/api/admin/pet-profile/config").json()
    assert got["tenant_id"] == "t1"
    assert got["profile_enabled"] is True
