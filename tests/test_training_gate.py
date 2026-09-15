"""S2 — trava da Capacitação nos aceites: bloqueia só com on + vet_approved + fim da carência.

Os passeios não existem de propósito: se a trava deixa passar, a rota segue e responde 404;
se bloqueia, responde 403 {"code": "training_required"} ANTES de buscar o passeio.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.dependencies.auth import get_current_user
from app.dependencies.training_gate import enforce_training_completed
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.routes import operational_walks
from tests.test_routes_walker_core import WALKER_ID, build
from tests.training_helpers import configure_training, make_bundle, write_bundle


def _content(tmp_path, monkeypatch, *, status="vet_approved", mode="on"):
    write_bundle(tmp_path, make_bundle(status=status))
    configure_training(monkeypatch, tmp_path, mode=mode)


def _mark_trained(db, version="9.0"):
    profile = db.query(WalkerProfile).filter(WalkerProfile.user_id == WALKER_ID).one()
    profile.training_completed_version = version
    db.commit()


def test_walker_accept_blocked_when_enforced_and_untrained(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch)
    client, _ = build()
    r = client.post("/walker/walks/nao-existe/accept")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "training_required"
    assert r.json()["detail"]["required_version"] == "9.0"


def test_walker_accept_passes_when_trained(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch)
    client, db = build()
    _mark_trained(db)
    assert client.post("/walker/walks/nao-existe/accept").status_code == 404


def test_walker_accept_not_blocked_with_draft_content(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch, status="draft")
    client, _ = build()
    assert client.post("/walker/walks/nao-existe/accept").status_code == 404


def test_walker_accept_not_blocked_in_warn_or_grace(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch, mode="warn")
    client, _ = build()
    assert client.post("/walker/walks/nao-existe/accept").status_code == 404
    configure_training(monkeypatch, tmp_path, mode="on", enforced_from="2999-01-01")
    assert client.post("/walker/walks/nao-existe/accept").status_code == 404


def test_walker_status_accept_branch_is_gated(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch)
    client, db = build()
    db.add(Walk(
        id="walk-gate", tutor_id="tutor-x", pet_id="pet-x", scheduled_date="2099-07-10T10:00:00",
        duration_minutes=45, price=49.9, status="Aguardando passeador",
        operational_status="pending_walker_confirmation", address_snapshot="Rua Teste, 1",
    ))
    db.commit()
    r = client.post("/walker/walks/walk-gate/status", json={"status": "walker_arriving"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "training_required"


def _operational_client(db):
    app = FastAPI()
    app.include_router(operational_walks.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(app)


def test_operational_accept_blocked_and_trained_passes(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch)
    _, db = build()
    client = _operational_client(db)
    r = client.post("/walks/nao-existe/accept")
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "training_required"
    _mark_trained(db)
    assert client.post("/walks/nao-existe/accept").status_code == 404


def test_gate_function_ignores_users_without_profile(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch)
    _, db = build(create_profile=False)
    enforce_training_completed(SimpleNamespace(id=WALKER_ID), db)  # não levanta


def test_gate_function_off_never_queries(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch, mode="off")

    class ExplodingDb:
        def query(self, *args, **kwargs):
            raise AssertionError("não deveria consultar com trava off")

    enforce_training_completed(SimpleNamespace(id="x"), ExplodingDb())


def test_gate_function_raises_http_403(tmp_path, monkeypatch):
    _content(tmp_path, monkeypatch)
    _, db = build()
    with pytest.raises(HTTPException) as exc:
        enforce_training_completed(db.get(User, WALKER_ID), db)
    assert exc.value.status_code == 403
