"""WK-06 — GET /walker/me/level expõe o nível real (espelha o dashboard)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import walker
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
WALKER_ID = "walker-test"


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="w@x.com", password_hash="x", role="walker", tenant_id=TENANT_ID, full_name="P"))
    db.add(WalkerProfile(id="wp-a", user_id=WALKER_ID, full_name="P", status="active", active_as_walker=True))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return test_app, db


def test_me_level_returns_level_object():
    test_app, _ = build()
    r = TestClient(test_app).get("/walker/me/level")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "current" in body and "name" in body["current"]
    assert "score" in body and "progress_percent" in body and "levels" in body


def test_me_level_matches_dashboard_level():
    test_app, _ = build()
    client = TestClient(test_app)
    lvl = client.get("/walker/me/level").json()
    ge = client.get("/walker/goals-evolution").json()["level"]
    assert lvl == ge


def test_me_level_requires_auth_401():
    test_app, _ = build()
    test_app.dependency_overrides.pop(get_current_user, None)
    r = TestClient(test_app).get("/walker/me/level")
    assert r.status_code == 401
