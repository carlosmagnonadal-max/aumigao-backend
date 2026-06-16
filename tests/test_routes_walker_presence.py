"""WK-02 — presença ONLINE/OFFLINE real (flag + last_seen).

Antes: não existia estado de presença no backend; o app guardava só localmente.
Agora POST /walker/online persiste is_online + last_seen_at, e o dashboard/availability
refletem o estado. É input real do matching (WK-10).
"""
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


def test_set_online_persists_flag_and_last_seen():
    test_app, db = build()
    client = TestClient(test_app)
    r = client.post("/walker/online", json={"online": True})
    assert r.status_code == 200, r.text
    assert r.json()["is_online"] is True
    prof = db.get(WalkerProfile, "wp-a")
    assert prof.is_online is True
    assert prof.last_seen_at is not None


def test_set_offline():
    test_app, db = build()
    client = TestClient(test_app)
    client.post("/walker/online", json={"online": True})
    r = client.post("/walker/online", json={"online": False})
    assert r.status_code == 200, r.text
    assert r.json()["is_online"] is False
    assert db.get(WalkerProfile, "wp-a").is_online is False


def test_availability_reflects_is_online():
    test_app, db = build()
    client = TestClient(test_app)
    client.post("/walker/online", json={"online": True})
    g = client.get("/walker/availability")
    assert g.status_code == 200, g.text
    assert g.json()["is_online"] is True


def test_online_invalid_payload_422():
    test_app, db = build()
    client = TestClient(test_app)
    r = client.post("/walker/online", json={"online": "talvez"})
    assert r.status_code == 422


def test_online_requires_auth_401():
    test_app, _ = build()
    test_app.dependency_overrides.pop(get_current_user, None)
    client = TestClient(test_app)
    r = client.post("/walker/online", json={"online": True})
    assert r.status_code == 401
