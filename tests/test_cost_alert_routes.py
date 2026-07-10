"""CRUD /api/admin/cost-alerts: validação, config_version, pause/resume,
summary com forecast e isolamento multi-tenant (404 cruzado)."""
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
from app.routes import cost_alerts

T_A, T_B = "t-a", "t-b"


def _build(admin_tenant=T_A):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.info["rls_tenant"] = "*"
    db.add(Tenant(id=T_A, name="A", slug="slug-a", status="active", plan="business"))
    db.add(Tenant(id=T_B, name="B", slug="slug-b", status="active", plan="business"))
    db.add(User(id="adm-a", email="a@a.com", password_hash="x", role="admin", tenant_id=T_A))
    db.add(User(id="adm-b", email="b@b.com", password_hash="x", role="admin", tenant_id=T_B))
    db.commit()
    test_app = FastAPI()
    test_app.include_router(cost_alerts.api_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, f"adm-{admin_tenant[-1]}")
    return test_app, db


_VALID = {
    "name": "Orçamento mensal", "scope": "total", "budget_amount": 500.0,
    "period": "monthly", "thresholds": [80, 100], "evaluation": "both",
    "channels": ["in_app", "email"],
}


def test_create_and_list_monthly_alert_80_100():
    test_app, db = _build()
    client = TestClient(test_app)
    r = client.post("/api/admin/cost-alerts", json=_VALID)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["thresholds"] == [80, 100]
    assert created["status"] == "active"
    listed = client.get("/api/admin/cost-alerts").json()
    assert len(listed) == 1
    assert "current_spend" in listed[0] and "forecast" in listed[0] and "next_threshold" in listed[0]


def test_validation_rejects_bad_payloads():
    test_app, db = _build()
    client = TestClient(test_app)
    assert client.post("/api/admin/cost-alerts", json={**_VALID, "budget_amount": 0}).status_code == 422
    assert client.post("/api/admin/cost-alerts", json={**_VALID, "thresholds": []}).status_code == 422
    assert client.post("/api/admin/cost-alerts", json={**_VALID, "thresholds": [80, 80]}).status_code == 422
    assert client.post("/api/admin/cost-alerts", json={**_VALID, "thresholds": [300]}).status_code == 422
    assert client.post("/api/admin/cost-alerts", json={**_VALID, "period": "hourly"}).status_code == 422
    assert client.post("/api/admin/cost-alerts", json={**_VALID, "channels": ["webhook"]}).status_code == 422


def test_edit_budget_bumps_config_version():
    test_app, db = _build()
    client = TestClient(test_app)
    alert_id = client.post("/api/admin/cost-alerts", json=_VALID).json()["id"]
    r = client.put(f"/api/admin/cost-alerts/{alert_id}", json={**_VALID, "budget_amount": 900.0})
    assert r.status_code == 200
    assert r.json()["config_version"] == 2
    # renomear NÃO bumpa
    r2 = client.put(f"/api/admin/cost-alerts/{alert_id}", json={**_VALID, "budget_amount": 900.0, "name": "Novo nome"})
    assert r2.json()["config_version"] == 2


def test_pause_resume_delete():
    test_app, db = _build()
    client = TestClient(test_app)
    alert_id = client.post("/api/admin/cost-alerts", json=_VALID).json()["id"]
    assert client.post(f"/api/admin/cost-alerts/{alert_id}/pause").json()["status"] == "paused"
    assert client.post(f"/api/admin/cost-alerts/{alert_id}/resume").json()["status"] == "active"
    assert client.delete(f"/api/admin/cost-alerts/{alert_id}").status_code == 204
    assert client.get("/api/admin/cost-alerts").json() == []


def test_cross_tenant_is_404():
    test_app, db = _build()
    client = TestClient(test_app)
    alert_id = client.post("/api/admin/cost-alerts", json=_VALID).json()["id"]
    # mesmo banco, admin do tenant B
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, "adm-b")
    client_b = TestClient(test_app)
    assert client_b.get(f"/api/admin/cost-alerts/{alert_id}/events").status_code == 404
    assert client_b.put(f"/api/admin/cost-alerts/{alert_id}", json=_VALID).status_code == 404
    assert client_b.delete(f"/api/admin/cost-alerts/{alert_id}").status_code == 404
    assert client_b.get("/api/admin/cost-alerts").json() == []
