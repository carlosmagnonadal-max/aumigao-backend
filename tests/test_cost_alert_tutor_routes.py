"""Fase 2: CRUD /cost-alerts do TUTOR — ownership, escopo pet, isolamento."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.pet import Pet
from app.models.tenant import Tenant
from app.models.user import User
from app.routes import cost_alerts

TENANT = "t-a"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.info["rls_tenant"] = "*"
    db.add(Tenant(id=TENANT, name="A", slug="slug-a", status="active", plan="business"))
    db.add(User(id="tut-1", email="t1@x.com", password_hash="x", role="cliente", tenant_id=TENANT))
    db.add(User(id="tut-2", email="t2@x.com", password_hash="x", role="cliente", tenant_id=TENANT))
    db.add(Pet(id="pet-a", tutor_id="tut-1", tenant_id=TENANT, name="Aurora"))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(cost_alerts.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, "tut-1")
    return test_app, db


_VALID = {"name": "Meu orçamento", "scope": "total", "budget_amount": 300.0,
          "period": "monthly", "thresholds": [80, 100], "evaluation": "both",
          "channels": ["in_app", "push"]}


def test_tutor_creates_and_lists_own_alert():
    test_app, db = _build()
    client = TestClient(test_app)
    r = client.post("/cost-alerts", json=_VALID)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scope"] == "total" and body["status"] == "active"
    listed = client.get("/cost-alerts").json()
    assert len(listed) == 1
    assert "current_spend" in listed[0] and "percent_used" in listed[0]


def test_pet_scope_accepted_and_unknown_pet_rejected():
    test_app, db = _build()
    client = TestClient(test_app)
    ok = client.post("/cost-alerts", json={**_VALID, "scope": "pet:pet-a"})
    assert ok.status_code == 201, ok.text
    bad = client.post("/cost-alerts", json={**_VALID, "scope": "pet:nao-existe"})
    assert bad.status_code == 422
    alheio = client.post("/cost-alerts", json={**_VALID, "scope": "own_walkers"})
    assert alheio.status_code == 422  # escopo de tenant não vale pro tutor


def test_ownership_cross_tutor_is_404():
    test_app, db = _build()
    client = TestClient(test_app)
    alert_id = client.post("/cost-alerts", json=_VALID).json()["id"]
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, "tut-2")
    other = TestClient(test_app)
    assert other.get(f"/cost-alerts/{alert_id}/events").status_code == 404
    assert other.put(f"/cost-alerts/{alert_id}", json=_VALID).status_code == 404
    assert other.delete(f"/cost-alerts/{alert_id}").status_code == 404
    assert other.get("/cost-alerts").json() == []


def test_pause_resume_delete_and_config_bump():
    test_app, db = _build()
    client = TestClient(test_app)
    alert_id = client.post("/cost-alerts", json=_VALID).json()["id"]
    assert client.post(f"/cost-alerts/{alert_id}/pause").json()["status"] == "paused"
    assert client.post(f"/cost-alerts/{alert_id}/resume").json()["status"] == "active"
    r = client.put(f"/cost-alerts/{alert_id}", json={**_VALID, "budget_amount": 500.0})
    assert r.json()["config_version"] == 2
    assert client.delete(f"/cost-alerts/{alert_id}").status_code == 204


def test_admin_route_does_not_list_tutor_alerts():
    """Isolamento fase1×fase2: GET /api/admin/cost-alerts NÃO devolve alertas de tutor."""
    test_app, db = _build()
    client = TestClient(test_app)
    client.post("/cost-alerts", json=_VALID)
    db.add(User(id="adm-1", email="a@x.com", password_hash="x", role="admin", tenant_id=TENANT))
    db.commit()
    test_app.include_router(cost_alerts.api_router)
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, "adm-1")
    assert TestClient(test_app).get("/api/admin/cost-alerts").json() == []
