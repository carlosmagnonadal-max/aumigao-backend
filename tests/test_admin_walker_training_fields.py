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


def test_list_reads_training_version_once_per_request_not_per_walker(tmp_path, monkeypatch):
    # S2-7: active_version() faz I/O de diretório (sem WALKER_TRAINING_REQUIRED_VERSION
    # fixada) — numa listagem com N passeadores, deve ser lido 1x, não N vezes.
    write_bundle(tmp_path, make_bundle(status="draft"))
    configure_training(monkeypatch, tmp_path, mode="warn")
    monkeypatch.delenv("WALKER_TRAINING_REQUIRED_VERSION", raising=False)
    db = make_db()
    db.add(User(id="walker-2", email="segundo@aumigao.com.br", password_hash="x", role="walker",
                full_name="Segundo Passeador", tenant_id="t-training", is_active=True))
    db.add(WalkerProfile(id="wp-2", user_id="walker-2", full_name="Segundo Passeador", city="Salvador",
                         state="BA", status="active", active_as_walker=True))
    db.commit()
    client = _client(db)

    from app.routes import admin as admin_routes

    calls = {"n": 0}
    real_active_version = admin_routes.training_content.active_version

    def counting_active_version():
        calls["n"] += 1
        return real_active_version()

    monkeypatch.setattr(admin_routes.training_content, "active_version", counting_active_version)
    r = client.get("/admin/partner-applications")
    assert r.status_code == 200, r.text
    assert len([row for row in r.json() if row.get("training_status")]) >= 2
    assert calls["n"] == 1
