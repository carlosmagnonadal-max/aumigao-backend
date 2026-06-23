"""WK-01 — disponibilidade real persistida (PUT grava, GET lê o gravado).

Antes: PUT /walker/availability só ecoava o payload (não persistia) e GET devolvia
semana fictícia. Agora há tabela WalkerAvailability (1 linha/walker) e o schedule
editável persiste, é lido de volta idêntico e é por-walker (deriva do token).
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walker_availability import WalkerAvailability
from app.models.walker_profile import WalkerProfile
from app.routes import walker
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-test"
WALKER_ID = "walker-test"
WALKER_B_ID = "walker-b"

SAMPLE_SCHEDULE = {
    "Seg": {"enabled": True, "slots": ["07:00", "08:00"]},
    "Ter": {"enabled": False, "slots": []},
}


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for uid, pid in ((WALKER_ID, "wp-a"), (WALKER_B_ID, "wp-b")):
        db.add(User(id=uid, email=f"{uid}@x.com", password_hash="x", role="walker", tenant_id=TENANT_ID, full_name="P"))
        db.add(WalkerProfile(id=pid, user_id=uid, full_name="P", status="active", active_as_walker=True))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(walker.router)
    test_app.dependency_overrides[get_db] = lambda: db
    # Passo 2: GET /walker/availability usa get_walker_self_db — injetar mesmo db.
    test_app.dependency_overrides[get_walker_self_db] = lambda: db
    return test_app, db


def as_walker(test_app, db, uid):
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, uid)
    return TestClient(test_app)


def test_put_availability_persists_and_get_returns_same():
    test_app, db = build()
    client = as_walker(test_app, db, WALKER_ID)
    r = client.put("/walker/availability", json={"schedule": SAMPLE_SCHEDULE})
    assert r.status_code == 200, r.text
    assert r.json()["schedule"]["Seg"]["slots"] == ["07:00", "08:00"]
    g = client.get("/walker/availability")
    assert g.status_code == 200, g.text
    assert g.json()["schedule"] == SAMPLE_SCHEDULE


def test_availability_persists_in_table():
    test_app, db = build()
    client = as_walker(test_app, db, WALKER_ID)
    client.put("/walker/availability", json={"schedule": SAMPLE_SCHEDULE})
    rows = db.query(WalkerAvailability).filter(WalkerAvailability.walker_user_id == WALKER_ID).all()
    assert len(rows) == 1  # uma linha por walker


def test_put_availability_is_upsert_not_duplicate():
    test_app, db = build()
    client = as_walker(test_app, db, WALKER_ID)
    client.put("/walker/availability", json={"schedule": SAMPLE_SCHEDULE})
    client.put("/walker/availability", json={"schedule": {"Qua": {"enabled": True, "slots": ["14:00"]}}})
    rows = db.query(WalkerAvailability).filter(WalkerAvailability.walker_user_id == WALKER_ID).all()
    assert len(rows) == 1
    g = client.get("/walker/availability")
    assert g.json()["schedule"] == {"Qua": {"enabled": True, "slots": ["14:00"]}}


def test_get_availability_empty_when_none_but_keeps_legacy_fields():
    test_app, db = build()
    client = as_walker(test_app, db, WALKER_ID)
    g = client.get("/walker/availability")
    assert g.status_code == 200, g.text
    body = g.json()
    assert body["schedule"] == {}  # honesto: vazio, não fictício
    # campos legados preservados (apps distribuídos antigos)
    assert "week" in body and "slots" in body and "month" in body


def test_availability_is_per_walker():
    test_app, db = build()
    as_walker(test_app, db, WALKER_ID).put("/walker/availability", json={"schedule": SAMPLE_SCHEDULE})
    # walker B não vê a disponibilidade de A
    gb = as_walker(test_app, db, WALKER_B_ID).get("/walker/availability")
    assert gb.json()["schedule"] == {}


def test_put_availability_invalid_payload_422():
    test_app, db = build()
    client = as_walker(test_app, db, WALKER_ID)
    r = client.put("/walker/availability", json={"schedule": {"Seg": {"enabled": "sim", "slots": "naoelista"}}})
    assert r.status_code == 422
