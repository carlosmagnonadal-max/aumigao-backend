"""S2 — rotas HTTP da Capacitação (app FastAPI mínimo, SQLite em memória; não importa app.main)."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.routes import walker_training
from tests.training_helpers import (
    M01_ANSWERS, TENANT_ID, WALKER_ID, configure_training, make_bundle, make_db, write_bundle,
)

ADMIN_ID = "admin-training"
TUTOR_ID = "tutor-training"


def build(tmp_path, monkeypatch, *, current_id=WALKER_ID, act_as_tenant=None):
    write_bundle(tmp_path, make_bundle(status="draft"))
    configure_training(monkeypatch, tmp_path, mode="warn")
    db = make_db()
    db.add(User(id=ADMIN_ID, email="admin@training.test", password_hash="x", role="super_admin"))
    db.add(User(id=TUTOR_ID, email="tutor@training.test", password_hash="x", role="tutor", tenant_id=TENANT_ID))
    db.commit()
    app = FastAPI()
    for router in (walker_training.router, walker_training.api_router,
                   walker_training.admin_router, walker_training.api_admin_router):
        app.include_router(router)
    current = db.get(User, current_id)
    if act_as_tenant:
        current._act_as_tenant_id = act_as_tenant
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: current
    return TestClient(app), db


def test_summary_and_api_alias(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch)
    r = client.get("/walker/training")
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["modules"]] == ["m01", "m08"]
    assert client.get("/api/walker/training").status_code == 200


def test_module_detail_never_exposes_answer_key(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch)
    r = client.get("/walker/training/modules/m01")
    assert r.status_code == 200, r.text
    assert "correct_index" not in r.text and "explanation" not in r.text
    assert client.get("/walker/training/modules/m99").status_code == 404


def test_quiz_submission(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch)
    r = client.post("/walker/training/modules/m01/quiz", json={"answers": M01_ANSWERS})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["passed"] is True and body["score"] == 100
    assert "correct_index" not in r.text
    assert client.post("/walker/training/modules/m01/quiz", json={}).status_code == 422
    assert client.post("/walker/training/modules/m01/quiz", json={"answers": [0]}).status_code == 422


def test_quick_guide_and_local_rules(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch)
    guide = client.get("/walker/training/quick-guide")
    assert guide.status_code == 200
    assert len(guide.json()["cards"]) == 2
    local = client.get("/walker/training/local-rules")
    assert local.status_code == 200
    assert [m["id"] for m in local.json()["modules"]] == ["m10b-salvador"]


def test_non_walker_is_forbidden(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, current_id=TUTOR_ID)
    assert client.get("/walker/training").status_code == 403
    assert client.post("/walker/training/modules/m01/quiz", json={"answers": M01_ANSWERS}).status_code == 403


def test_admin_detail_super_admin(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, current_id=ADMIN_ID)
    r = client.get(f"/admin/walkers/{WALKER_ID}/training")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["version"] == "9.0"
    assert {m["id"] for m in body["modules"]} == {"m01", "m08", "m10b-salvador"}
    assert "correct_index" not in r.text
    assert client.get("/api/admin/walkers/nao-existe/training").status_code == 404


def test_admin_detail_requires_permission(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, current_id=TUTOR_ID)
    assert client.get(f"/admin/walkers/{WALKER_ID}/training").status_code == 403


def test_admin_scoped_to_other_tenant_gets_404(tmp_path, monkeypatch):
    client, _ = build(tmp_path, monkeypatch, current_id=ADMIN_ID, act_as_tenant="outro-tenant")
    assert client.get(f"/admin/walkers/{WALKER_ID}/training").status_code == 404
