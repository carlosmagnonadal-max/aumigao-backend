from datetime import date
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import walker as walker_routes


def _build():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    user = User(
        id="w1",
        email="w1@t.invalid",
        password_hash="x",
        role="walker",
        is_active=True,
        token_version=0,
        must_change_password=False,
    )
    db.add(user)
    # _require_active_walker precisa de profile com status="active" e active_as_walker=True
    profile = WalkerProfile(
        id="p1",
        user_id="w1",
        status="active",
        active_as_walker=True,
    )
    db.add(profile)
    db.commit()
    app = FastAPI()
    app.include_router(walker_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_walker_self_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, "w1")
    return TestClient(app), db


def test_create_list_delete_exception():
    client, db = _build()
    r = client.post(
        "/walker/availability/exceptions",
        json={"exception_date": "2099-01-10", "kind": "block"},
    )
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    r = client.get(
        "/walker/availability/exceptions",
        params={"from": "2099-01-01", "to": "2099-01-31"},
    )
    assert r.status_code == 200
    assert any(e["id"] == eid for e in r.json())
    r = client.delete(f"/walker/availability/exceptions/{eid}")
    assert r.status_code == 200
    r = client.get(
        "/walker/availability/exceptions",
        params={"from": "2099-01-01", "to": "2099-01-31"},
    )
    assert all(e["id"] != eid for e in r.json())


def test_invalid_kind_400():
    client, db = _build()
    r = client.post(
        "/walker/availability/exceptions",
        json={"exception_date": "2099-01-10", "kind": "xpto"},
    )
    assert r.status_code == 400
