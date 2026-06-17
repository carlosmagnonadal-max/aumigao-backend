"""Testes de ROTA (camada HTTP) do grupo "aprovacao de passeador" em
app/routes/admin.py.

Cobre o wiring real dos endpoints de candidatura/aprovacao de passeador:
- GET  /admin/partner-applications              (listagem; so admin.access do router)
- GET  /admin/partner-applications/{id}         (detalhe + 404)
- PATCH /admin/partner-applications/{id}/admin-fields  (status/notas/ativar walker)
- POST /admin/walkers/{id}/approve              (transicao -> approved)
- POST /admin/walkers/{id}/reject               (transicao -> rejected + reason)

Padrao do projeto (ver tests/test_routes_walker_quality.py e test_routes_auth.py):
monta um FastAPI MINIMO com apenas o router de admin, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main
(que conecta no Neon de PROD).

Permissao: o admin.router tem dependencia de require_permission("admin.access")
no nivel do router, e os endpoints de escrita exigem walkers.validate. Um User
role="super_admin" passa em ambos (atalho de rede de seguranca em rbac); um
"tutor" e barrado com 403.

IMPORTANTE: os helpers internos filtram entidades "fake" por tokens (test, demo,
seed, login, etc — ver FAKE_ENTITY_TOKENS). Por isso usamos nomes/emails/ids
neutros que NAO contenham esses tokens, senao o candidato some da listagem.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import admin

ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"
CAND_ID = "cand-1"
CAND_USER_ID = "candidato-user-1"


def build(*, current: str = ADMIN_ID, profile_status: str = "submitted"):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin -> passa em admin.access e walkers.validate (atalho RBAC)
    db.add(User(id=ADMIN_ID, email="adm@correio.com", password_hash="x", role="super_admin", full_name="Administrador"))
    # tutor comum -> sem permissao -> 403
    db.add(User(id=TUTOR_ID, email="cliente@correio.com", password_hash="x", role="tutor", full_name="Maria Cliente"))
    # candidato a passeador (usuario + perfil). Nomes/emails neutros (sem tokens fake).
    db.add(User(id=CAND_USER_ID, email="joao.silva@correio.com", password_hash="x", role="cliente", full_name="Joao Silva"))
    db.add(WalkerProfile(
        id=CAND_ID, user_id=CAND_USER_ID, full_name="Joao Silva",
        cpf="52998224725", phone="11987654321", city="Sao Paulo", state="SP",
        status=profile_status, created_at=datetime.utcnow(),
    ))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


# ------------------------------------------------- GET partner-applications ---
def test_partner_applications_lists_candidate():
    client, _ = build()
    r = client.get("/admin/partner-applications")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [item["walker_id"] for item in body]
    assert CAND_ID in ids


def test_partner_applications_requires_admin_access():
    # tutor nao tem admin.access (dependencia do router) -> 403
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/partner-applications")
    assert r.status_code == 403


def test_partner_application_detail_happy_path():
    client, _ = build()
    r = client.get(f"/admin/partner-applications/{CAND_ID}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["walker_id"] == CAND_ID
    assert body["user_id"] == CAND_USER_ID
    assert body["raw_status"] == "submitted"
    # Ficha do passeador (task 3): porte máximo aceito + veículo próprio serializados.
    assert "max_dog_size" in body
    assert "has_vehicle" in body
    assert isinstance(body["has_vehicle"], bool)


def test_partner_application_detail_404():
    client, _ = build()
    r = client.get("/admin/partner-applications/inexistente")
    assert r.status_code == 404


# --------------------------------------------------------- approve / reject ---
def test_approve_walker_happy_path():
    # Aprovacao em UM passo: aprova E ativa operacionalmente (libera o passeador
    # no app) — status=active, active_as_walker=True e role do user vira "walker".
    client, db = build(profile_status="under_review")
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raw_status"] == "active"
    assert body["approved_at"] is not None
    assert body["rejected_at"] is None
    # persistencia real: ativo, promovido a walker
    prof = db.get(WalkerProfile, CAND_ID)
    assert prof.status == "active"
    assert prof.active_as_walker is True
    assert db.get(User, CAND_USER_ID).role == "walker"


def test_approve_walker_requires_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.post(f"/admin/walkers/{CAND_ID}/approve")
    assert r.status_code == 403


def test_approve_walker_404_unknown():
    client, _ = build()
    r = client.post("/admin/walkers/inexistente/approve")
    assert r.status_code == 404


def test_reject_walker_sets_reason():
    client, db = build(profile_status="under_review")
    r = client.post(f"/admin/walkers/{CAND_ID}/reject", json={"reason": "Documentos ilegiveis"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raw_status"] == "rejected"
    assert body["rejected_at"] is not None
    assert body["rejection_reason"] == "Documentos ilegiveis"
    prof = db.get(WalkerProfile, CAND_ID)
    assert prof.status == "rejected"
    assert prof.active_as_walker is False


def test_reject_walker_without_reason_ok():
    client, _ = build(profile_status="under_review")
    r = client.post(f"/admin/walkers/{CAND_ID}/reject")
    assert r.status_code == 200, r.text
    assert r.json()["raw_status"] == "rejected"


def test_reject_walker_requires_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.post(f"/admin/walkers/{CAND_ID}/reject", json={"reason": "x"})
    assert r.status_code == 403


def test_reject_walker_404_unknown():
    client, _ = build()
    r = client.post("/admin/walkers/inexistente/reject")
    assert r.status_code == 404


# ------------------------------------------------------------- admin-fields ---
def test_admin_fields_change_status_to_approved():
    client, db = build(profile_status="submitted")
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/admin-fields",
        json={"status": "approved"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["raw_status"] == "approved"
    assert db.get(WalkerProfile, CAND_ID).status == "approved"


def test_admin_fields_set_internal_notes():
    client, db = build()
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/admin-fields",
        json={"internal_notes": "Ligar para confirmar endereco"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["internal_notes"] == "Ligar para confirmar endereco"
    assert db.get(WalkerProfile, CAND_ID).internal_notes == "Ligar para confirmar endereco"


def test_admin_fields_activate_as_walker_promotes_role():
    # candidato ja aprovado -> ativar como walker muda role do user para "walker"
    client, db = build(profile_status="approved")
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/admin-fields",
        json={"active_as_walker": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["raw_status"] == "active"
    assert body["active_as_walker"] is True
    assert db.get(User, CAND_USER_ID).role == "walker"
    assert db.get(WalkerProfile, CAND_ID).active_as_walker is True


def test_admin_fields_activate_requires_approved_status():
    # candidato em "submitted" nao pode ser ativado -> 400 (transicao invalida)
    client, _ = build(profile_status="submitted")
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/admin-fields",
        json={"active_as_walker": True},
    )
    assert r.status_code == 400
    assert "aprovados" in r.json()["detail"].lower()


def test_admin_fields_requires_permission():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.patch(
        f"/admin/partner-applications/{CAND_ID}/admin-fields",
        json={"status": "approved"},
    )
    assert r.status_code == 403


def test_admin_fields_404_unknown():
    client, _ = build()
    r = client.patch("/admin/partner-applications/inexistente/admin-fields", json={"status": "approved"})
    assert r.status_code == 404
