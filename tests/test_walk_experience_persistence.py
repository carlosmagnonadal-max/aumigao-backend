"""Mig 0104: POST /walker/walks/{id}/experience deve PERSISTIR did_pee/did_poop.

Antes os valores eram apenas logados e ecoados no JSON — o tutor perdia os
eventos a cada reload (bug do teste real de 08/07)."""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.routes import walker
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-exp"
WALKER_ID = "walker-exp"
TUTOR_ID = "tutor-exp"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="w@exp.com", password_hash="x", role="walker", tenant_id=TENANT_ID, full_name="Walker Exp"))
    db.add(User(id=TUTOR_ID, email="t@exp.com", password_hash="x", role="tutor", tenant_id=TENANT_ID, full_name="Tutor Exp"))
    db.add(WalkerProfile(id="wp-exp", user_id=WALKER_ID, full_name="Walker Exp", status="active", active_as_walker=True))
    db.add(Walk(
        id="walk-exp-1", tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walker_id=WALKER_ID, pet_id="pet-1",
        scheduled_date="2024-06-01T10:00:00", duration_minutes=30, price=50.0,
        status="Passeando agora", operational_status="ride_in_progress", created_at=datetime.utcnow(),
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_walker_self_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(test_app), db


def test_experience_persists_on_walk_row():
    client, db = _build()
    r = client.post("/walker/walks/walk-exp-1/experience", json={"did_pee": True, "did_poop": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["did_pee"] is True
    assert body["did_poop"] is False

    db.expire_all()
    walk = db.get(Walk, "walk-exp-1")
    assert walk.did_pee is True
    assert walk.did_poop is False


def test_experience_default_null_means_not_reported():
    client, db = _build()
    walk = db.get(Walk, "walk-exp-1")
    assert walk.did_pee is None
    assert walk.did_poop is None


def test_walk_response_declares_report_fields():
    """Gotcha do response_model: sem declarar, o Pydantic descarta o que o
    serializador emite. Estes campos alimentam o relatório do tutor."""
    from app.schemas.walk import WalkResponse

    for field in ("completion_review", "did_pee", "did_poop"):
        assert field in WalkResponse.model_fields, field
