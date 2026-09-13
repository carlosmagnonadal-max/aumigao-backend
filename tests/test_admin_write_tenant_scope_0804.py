"""Sec-audit 2026-08-04, achado #2 (alto): endpoints admin de ESCRITA sem escopo de tenant.

Endpoints corrigidos (padrao de approve_walker / PATCH /admin/walkers/{id}/wallet:
get_admin_tenant_scope no topo; recurso de outro tenant -> 404; super_admin global):
  1. POST /admin/walker-kits/{submission_id}/approve
  2. POST /admin/walker-kits/{submission_id}/reject
  3. POST /admin/walker-programs/walkers/{walker_id}/cr
  4. POST /admin/walker-programs/walkers/{walker_id}/kit-audit
  5. POST /admin/walker-programs/tips/{tip_id}/review
  6. POST /api/admin/product-highlights/upload-photo  (sem recurso por id: escopo no
     topo + upload_files gravado com o tenant do escopo)

Padrao: FastAPI minimo + SQLite em memoria (StaticPool). NUNCA importa app.main.
"""
from __future__ import annotations

import io
from datetime import datetime
from unittest.mock import patch

import app.models  # noqa: F401 — registra todas as tabelas
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user, require_admin
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.tenant import Tenant
from app.models.upload_file import UploadFile as UploadFileRow
from app.models.user import User
from app.models.walk_tip import WalkTip
from app.models.walker_kit_submission import WalkerKitSubmission
from app.models.walker_profile import WalkerProfile
from app.models.walker_program_action import WalkerProgramAction
from app.routes import admin
from app.routes import product_highlights

T_A = "tenant-a"
T_B = "tenant-b"
SUPER_ID = "super-1"
ADMIN_A = "admin-a"
ADMIN_B = "admin-b"
WALKER_A_USER = "walker-a"
WALKER_A_PROFILE = "wp-a"
KIT_A = "kit-a"
TIP_A = "tip-a"

PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def _grant(db, user_id: str, tenant_id: str, *keys: str) -> None:
    role = Role(name=f"tenant_admin-{user_id}", scope_type="tenant")
    db.add(role)
    db.flush()
    for key in keys:
        perm = db.query(Permission).filter(Permission.key == key).first()
        if not perm:
            perm = Permission(key=key, module=key.split(".")[0], action=key.split(".")[-1])
            db.add(perm)
            db.flush()
        db.add(RolePermission(role_id=role.id, permission_id=perm.id))
    db.add(UserRoleAssignment(user_id=user_id, role_id=role.id, tenant_id=tenant_id))
    db.commit()


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id=T_A, name="A", slug="tenant-a", status="active", plan="enterprise"))
    db.add(Tenant(id=T_B, name="B", slug="tenant-b", status="active", plan="enterprise"))
    db.add(User(id=SUPER_ID, email="super@x.com", password_hash="x", role="super_admin"))
    db.add(User(id=ADMIN_A, email="aa@x.com", password_hash="x", role="admin", tenant_id=T_A))
    db.add(User(id=ADMIN_B, email="ab@x.com", password_hash="x", role="admin", tenant_id=T_B))
    db.add(User(id=WALKER_A_USER, email="wa@x.com", password_hash="x", role="walker", tenant_id=T_A,
                full_name="Walker A"))
    db.add(WalkerProfile(id=WALKER_A_PROFILE, user_id=WALKER_A_USER, full_name="Walker A", status="approved"))
    now = datetime.utcnow()
    db.add(WalkerKitSubmission(id=KIT_A, walker_user_id=WALKER_A_USER, items_json="{}",
                               audit_status="pending_review", audit_note="", created_at=now, updated_at=now))
    db.add(WalkTip(id=TIP_A, tenant_id=T_A, walk_id="walk-a", tutor_id="tutor-a", walker_id=WALKER_A_USER,
                   amount=10, status="pending_review"))
    db.commit()
    for uid, tid in ((ADMIN_A, T_A), (ADMIN_B, T_B)):
        _grant(db, uid, tid, "admin.access", "walkers.validate", "walkers.read")

    app_ = FastAPI()
    app_.include_router(admin.router)
    app_.dependency_overrides[get_db] = lambda: db
    client = TestClient(app_)
    return client, db


def _as(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)
    return client


def _actions(db) -> int:
    return db.query(WalkerProgramAction).count()


# ---------------------------------------------------------------------------
# 1-2. walker-kits approve / reject
# ---------------------------------------------------------------------------

def test_kit_approve_cross_tenant_404_and_untouched():
    client, db = _build()
    r = _as(client, db, ADMIN_B).post(f"/admin/walker-kits/{KIT_A}/approve")
    assert r.status_code == 404, r.text
    db.expire_all()
    assert db.get(WalkerKitSubmission, KIT_A).audit_status == "pending_review"


def test_kit_approve_same_tenant_200():
    client, db = _build()
    r = _as(client, db, ADMIN_A).post(f"/admin/walker-kits/{KIT_A}/approve")
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(WalkerKitSubmission, KIT_A).audit_status == "approved"


def test_kit_reject_cross_tenant_404_and_untouched():
    client, db = _build()
    r = _as(client, db, ADMIN_B).post(f"/admin/walker-kits/{KIT_A}/reject", json={"reason": "x"})
    assert r.status_code == 404, r.text
    db.expire_all()
    assert db.get(WalkerKitSubmission, KIT_A).audit_status == "pending_review"


def test_kit_reject_same_tenant_200():
    client, db = _build()
    r = _as(client, db, ADMIN_A).post(f"/admin/walker-kits/{KIT_A}/reject", json={"reason": "foto ruim"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(WalkerKitSubmission, KIT_A).audit_status == "rejected"


def test_kit_approve_super_admin_global_200():
    client, db = _build()
    r = _as(client, db, SUPER_ID).post(f"/admin/walker-kits/{KIT_A}/approve")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 3. walker-programs CR
# ---------------------------------------------------------------------------

def test_cr_cross_tenant_404_no_action():
    client, db = _build()
    r = _as(client, db, ADMIN_B).post(f"/admin/walker-programs/walkers/{WALKER_A_USER}/cr",
                                      json={"amount": 50, "reason": "x"})
    assert r.status_code == 404, r.text
    assert _actions(db) == 0


def test_cr_unknown_walker_tenant_admin_404():
    client, db = _build()
    r = _as(client, db, ADMIN_A).post("/admin/walker-programs/walkers/nao-existe/cr", json={"amount": 1})
    assert r.status_code == 404, r.text
    assert _actions(db) == 0


def test_cr_same_tenant_200_by_user_id_and_profile_id():
    client, db = _build()
    c = _as(client, db, ADMIN_A)
    r1 = c.post(f"/admin/walker-programs/walkers/{WALKER_A_USER}/cr", json={"amount": 5})
    r2 = c.post(f"/admin/walker-programs/walkers/{WALKER_A_PROFILE}/cr", json={"amount": 5})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert _actions(db) == 2


# ---------------------------------------------------------------------------
# 4. walker-programs kit-audit
# ---------------------------------------------------------------------------

def test_kit_audit_cross_tenant_404_no_action():
    client, db = _build()
    r = _as(client, db, ADMIN_B).post(f"/admin/walker-programs/walkers/{WALKER_A_USER}/kit-audit",
                                      json={"status": "reprovado", "note": "x"})
    assert r.status_code == 404, r.text
    assert _actions(db) == 0


def test_kit_audit_same_tenant_200():
    client, db = _build()
    r = _as(client, db, ADMIN_A).post(f"/admin/walker-programs/walkers/{WALKER_A_USER}/kit-audit",
                                      json={"status": "aprovado", "note": "ok"})
    assert r.status_code == 200, r.text
    assert _actions(db) == 1


# ---------------------------------------------------------------------------
# 5. walker-programs tip review
# ---------------------------------------------------------------------------

def test_tip_review_cross_tenant_404_no_action():
    client, db = _build()
    r = _as(client, db, ADMIN_B).post(f"/admin/walker-programs/tips/{TIP_A}/review",
                                      json={"status": "rejected", "note": "x"})
    assert r.status_code == 404, r.text
    assert _actions(db) == 0


def test_tip_review_unknown_tip_tenant_admin_404():
    client, db = _build()
    r = _as(client, db, ADMIN_A).post("/admin/walker-programs/tips/nao-existe/review", json={})
    assert r.status_code == 404, r.text


def test_tip_review_same_tenant_200():
    client, db = _build()
    r = _as(client, db, ADMIN_A).post(f"/admin/walker-programs/tips/{TIP_A}/review",
                                      json={"status": "approved", "note": "ok"})
    assert r.status_code == 200, r.text
    assert _actions(db) == 1


def test_programs_super_admin_global_keeps_access():
    client, db = _build()
    c = _as(client, db, SUPER_ID)
    assert c.post(f"/admin/walker-programs/walkers/{WALKER_A_USER}/cr", json={"amount": 1}).status_code == 200
    assert c.post(f"/admin/walker-programs/walkers/{WALKER_A_USER}/kit-audit", json={}).status_code == 200
    assert c.post(f"/admin/walker-programs/tips/{TIP_A}/review", json={}).status_code == 200


# ---------------------------------------------------------------------------
# 6. product-highlights upload-photo
# ---------------------------------------------------------------------------

def _upload(db, user, monkeypatch, tmp_path):
    monkeypatch.setattr("app.routes.product_highlights._HIGHLIGHT_UPLOAD_ROOT", tmp_path / "ph")
    monkeypatch.setattr("app.routes.product_highlights.UPLOADS_BASE", tmp_path)
    app_ = FastAPI()
    app_.include_router(product_highlights.api_router)
    app_.dependency_overrides[get_db] = lambda: db
    app_.dependency_overrides[require_admin] = lambda: user
    app_.dependency_overrides[get_current_user] = lambda: user
    with patch("app.routes.product_highlights.object_storage.save", return_value=None), \
         patch("app.routes.product_highlights.get_admin_tenant_scope",
               wraps=product_highlights.get_admin_tenant_scope) as spy:
        r = TestClient(app_).post(
            "/api/admin/product-highlights/upload-photo",
            files={"file": ("foto.png", io.BytesIO(PNG_1x1), "image/png")},
        )
    return r, spy


def test_upload_photo_tenant_admin_records_own_tenant(monkeypatch, tmp_path):
    _, db = _build()
    r, spy = _upload(db, db.get(User, ADMIN_B), monkeypatch, tmp_path)
    assert r.status_code == 201, r.text
    assert spy.called  # escopo resolvido (GUC RLS) antes do INSERT
    rows = db.query(UploadFileRow).filter(UploadFileRow.context == "product_highlight").all()
    assert len(rows) == 1
    assert rows[0].tenant_id == T_B  # nunca NULL / nunca outro tenant


def test_upload_photo_super_admin_global_allowed(monkeypatch, tmp_path):
    _, db = _build()
    r, _spy = _upload(db, db.get(User, SUPER_ID), monkeypatch, tmp_path)
    assert r.status_code == 201, r.text
    row = db.query(UploadFileRow).filter(UploadFileRow.context == "product_highlight").one()
    assert row.tenant_id is None


def test_upload_photo_admin_without_tenant_rejected(monkeypatch, tmp_path):
    _, db = _build()
    db.add(User(id="orphan", email="o@x.com", password_hash="x", role="admin", tenant_id=None))
    db.commit()
    r, _spy = _upload(db, db.get(User, "orphan"), monkeypatch, tmp_path)
    assert r.status_code == 400, r.text
    assert db.query(UploadFileRow).count() == 0
