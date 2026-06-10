"""Testes de ROTA (camada HTTP) do grupo "kit-walker" de app/routes/admin.py.

Cobre a auditoria administrativa de KIT do passeador:
- GET  /admin/walker-kits/pending          (lista submissoes pending_review)
- POST /admin/walker-kits/{id}/approve      (require_permission("walkers.validate"))
- POST /admin/walker-kits/{id}/reject       (require_permission("walkers.validate"))

Padrao do projeto (ver tests/test_routes_walker_quality.py / test_routes_auth.py):
monta um FastAPI MINIMO so com o router de admin (app.routes.admin.router, prefix
/admin) + overrides de get_db / get_current_user. NAO importa app.main (Neon PROD).

Gating:
- O router /admin tem dependency de nivel de router require_permission("admin.access").
- approve/reject tem dependency adicional require_permission("walkers.validate").
- super_admin (role string) bypassa RBAC via rede de seguranca em
  app/dependencies/rbac.user_has_permission -> usado no happy path.
- role "tutor" (sem RBAC seed) cai no 403.
"""
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_profile import WalkerProfile
from app.routes import admin

ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"
WALKER_ID = "walker-1"


def build(*, current: str = ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin -> bypassa RBAC (admin.access + walkers.validate)
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    # tutor comum -> sem permissao (403)
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor"))
    # passeador com perfil, dono do kit
    db.add(User(id=WALKER_ID, email="walker@test.com", password_hash="x", role="walker", full_name="Joao Passeador"))
    db.add(WalkerProfile(id="wp-1", user_id=WALKER_ID, full_name="Joao Passeador", status="approved"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


def add_submission(db, *, sid="kit-1", walker_user_id=WALKER_ID, audit_status="pending_review", items='{"colete": true}'):
    sub = WalkerKitSubmission(
        id=sid,
        walker_user_id=walker_user_id,
        items_json=items,
        audit_status=audit_status,
        audit_note="",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(sub)
    db.commit()
    return sub


# ----------------------------------------------- GET /walker-kits/pending ---
def test_pending_lists_only_pending_review():
    client, db = build()
    add_submission(db, sid="kit-pending", audit_status="pending_review")
    add_submission(db, sid="kit-approved", walker_user_id="other-walker", audit_status="approved")
    r = client.get("/admin/walker-kits/pending")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    ids = [item["id"] for item in body["items"]]
    assert ids == ["kit-pending"]
    # serializacao do _serialize_walker_kit_submission
    item = body["items"][0]
    assert item["walker_user_id"] == WALKER_ID
    assert item["walker_name"] == "Joao Passeador"
    assert item["audit_status"] == "pending_review"


def test_pending_empty():
    client, _ = build()
    body = client.get("/admin/walker-kits/pending").json()
    assert body == {"items": [], "total": 0}


def test_pending_requires_admin_access():
    # router-level require_permission("admin.access") -> tutor 403
    client, db = build()
    set_user(client, db, TUTOR_ID)
    assert client.get("/admin/walker-kits/pending").status_code == 403


# ------------------------------------------ POST /walker-kits/{id}/approve ---
def test_approve_happy_path():
    client, db = build()
    add_submission(db, sid="kit-a")
    r = client.post("/admin/walker-kits/kit-a/approve")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "kit-a"
    assert body["audit_status"] == "approved"
    assert body["audit_note"] == "Kit aprovado pela auditoria administrativa."
    assert body["reviewed_by_admin_id"] == ADMIN_ID
    assert body["reviewed_at"] is not None
    # persistido
    db.expire_all()
    sub = db.query(WalkerKitSubmission).filter_by(id="kit-a").first()
    assert sub.audit_status == "approved"


def test_approve_404_unknown_submission():
    client, _ = build()
    r = client.post("/admin/walker-kits/nao-existe/approve")
    assert r.status_code == 404


def test_approve_forbidden_for_tutor():
    # walkers.validate (e admin.access) negado para tutor
    client, db = build()
    add_submission(db, sid="kit-fb")
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/walker-kits/kit-fb/approve")
    assert r.status_code == 403


# ------------------------------------------- POST /walker-kits/{id}/reject ---
def test_reject_happy_path_with_audit_note():
    client, db = build()
    add_submission(db, sid="kit-r")
    r = client.post("/admin/walker-kits/kit-r/reject", json={"audit_note": "Colete faltando"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audit_status"] == "rejected"
    assert body["audit_note"] == "Colete faltando"
    assert body["reviewed_by_admin_id"] == ADMIN_ID


def test_reject_accepts_reason_alias():
    # payload aceita "reason" como alias de "audit_note"
    client, db = build()
    add_submission(db, sid="kit-r2")
    r = client.post("/admin/walker-kits/kit-r2/reject", json={"reason": "Documento ilegivel"})
    assert r.status_code == 200, r.text
    assert r.json()["audit_note"] == "Documento ilegivel"


def test_reject_without_body_uses_default_note():
    client, db = build()
    add_submission(db, sid="kit-r3")
    r = client.post("/admin/walker-kits/kit-r3/reject")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["audit_status"] == "rejected"
    assert body["audit_note"] == "Kit rejeitado pela auditoria administrativa."


def test_reject_404_unknown_submission():
    client, _ = build()
    r = client.post("/admin/walker-kits/nao-existe/reject", json={"reason": "x"})
    assert r.status_code == 404


def test_reject_forbidden_for_tutor():
    client, db = build()
    add_submission(db, sid="kit-rfb")
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/walker-kits/kit-rfb/reject", json={"reason": "x"})
    assert r.status_code == 403


# ---- transicao: approve depois reject (audit_status reflete ultima acao) ----
def test_audit_status_transitions_on_re_audit():
    client, db = build()
    add_submission(db, sid="kit-t")
    assert client.post("/admin/walker-kits/kit-t/approve").json()["audit_status"] == "approved"
    # auditor pode reverter para rejected (sem guard de transicao no endpoint)
    assert client.post("/admin/walker-kits/kit-t/reject", json={"reason": "revisao"}).json()["audit_status"] == "rejected"
