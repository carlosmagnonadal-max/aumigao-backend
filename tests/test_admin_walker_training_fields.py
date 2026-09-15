"""S2 — admin vê status da Capacitação na lista e no detalhe de passeadores; LGPD exporta as colunas."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import admin as admin_routes
from app.services.data_subject_rights_service import _WALKER_PROFILE_FIELDS
from tests.training_helpers import PROFILE_ID, WALKER_ID, configure_training, make_bundle, make_db, write_bundle

ADMIN_ID = "admin-fields"


def _client(db):
    db.add(User(id=ADMIN_ID, email="adm@fields.test", password_hash="x", role="super_admin"))
    db.commit()
    app = FastAPI()
    app.include_router(admin_routes.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(app)


def test_detail_shows_pending_then_completed(tmp_path, monkeypatch):
    write_bundle(tmp_path, make_bundle(status="draft"))
    configure_training(monkeypatch, tmp_path, mode="warn")
    db = make_db()
    client = _client(db)

    body = client.get(f"/admin/partner-applications/{PROFILE_ID}").json()
    assert body["training_status"] == "pending"
    assert body["training_completed_version"] is None

    profile = db.query(WalkerProfile).filter_by(user_id=WALKER_ID).one()
    profile.training_completed_version = "9.0"
    db.commit()
    body = client.get(f"/admin/partner-applications/{PROFILE_ID}").json()
    assert body["training_status"] == "completed"


def test_list_includes_training_status_and_unavailable_without_bundle(tmp_path, monkeypatch):
    configure_training(monkeypatch, tmp_path / "vazio", mode="off")
    monkeypatch.delenv("WALKER_TRAINING_REQUIRED_VERSION", raising=False)
    db = make_db()
    client = _client(db)
    r = client.get("/admin/partner-applications")
    assert r.status_code == 200, r.text
    rows = [row for row in r.json() if row.get("user_id") == WALKER_ID]
    assert rows and rows[0]["training_status"] == "unavailable"


def test_lgpd_export_includes_training_columns():
    assert {"training_completed_version", "training_completed_at"} <= set(_WALKER_PROFILE_FIELDS)
