"""Testes do fix: métricas e listagem de tutores no Modelo B (white-label multi-tenant).

Cenários cobertos:
  T1  tutor nascido no tenant conta 1
  T2  tutor vinculado (TenantTutorAccess ativo, nascido em outro tenant) conta 1
  T3  tutor com nascimento + vínculo ativo conta 1 (sem dupla contagem)
  T4  access inativo (pending/revoked) NÃO conta
  T5  listagem GET /admin/tutors inclui tutor vinculado
  T6  listagem GET /admin/tutors exclui acesso inativo
  T7  escopo global não é afetado pelo join (continua contando por User.tenant_id)

Pontos corrigidos:
  - app/routes/admin.py: _sql_count_real_tutors (dashboard total_tutors / total_clients)
  - app/routes/admin.py: GET /admin/tutors (listagem)
"""
from __future__ import annotations

import app.models  # noqa: F401 — registra todos os modelos no Base.metadata

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.dependencies.tenant_scope import AdminTenantScope
from app.models.tenant import Tenant
from app.models.tenant_tutor_access import TenantTutorAccess
from app.models.user import User
from app.routes import admin as admin_routes
from app.routes.admin import _sql_count_real_tutors

# ── Constantes ───────────────────────────────────────────────────────────────

TENANT_A = "ta"
TENANT_B = "tb"

# E-mails sem tokens fake ("test", "demo", "login", "mock") → passam no realness.
EMAIL_A = "tutor-a@aumigao.app"
EMAIL_B = "tutor-b@aumigao.app"
EMAIL_C = "tutor-c@aumigao.app"


# ── Infra de DB ───────────────────────────────────────────────────────────────

def _make_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Sm = sessionmaker(bind=engine)
    db = Sm()
    db.add(Tenant(id=TENANT_A, name="Tenant A", slug="ta", status="active", plan="pro"))
    db.add(Tenant(id=TENANT_B, name="Tenant B", slug="tb", status="active", plan="pro"))
    db.commit()
    return db


def _scope(tenant_id: str) -> AdminTenantScope:
    """Scope de tenant (não-global)."""
    admin_user = User(id="adm", email="admin@aumigao.app", password_hash="x", role="super_admin")
    admin_user._act_as_tenant_id = tenant_id
    return AdminTenantScope(user=admin_user, tenant_id=tenant_id, is_global=False, role="super_admin")


def _global_scope() -> AdminTenantScope:
    """Scope global (super_admin sem tenant)."""
    admin_user = User(id="adm", email="admin@aumigao.app", password_hash="x", role="super_admin")
    return AdminTenantScope(user=admin_user, tenant_id=None, is_global=True, role="super_admin")


def _add_tutor(db: Session, *, uid: str, email: str, tenant_id: str) -> User:
    u = User(id=uid, email=email, password_hash="x", role="tutor",
             full_name="Tutor Real", tenant_id=tenant_id)
    db.add(u)
    db.commit()
    return u


def _add_access(db: Session, *, tenant_id: str, tutor_user_id: str, status: str = "active") -> None:
    db.add(TenantTutorAccess(
        id=str(uuid4()),
        tenant_id=tenant_id,
        tutor_user_id=tutor_user_id,
        status=status,
        initiated_by="tutor",
    ))
    db.commit()


# ── TestClient do endpoint /admin/tutors ──────────────────────────────────────

def _client_for(db: Session, *, tenant_id: str) -> TestClient:
    """Cliente HTTP operando como super_admin act-as tenant_id."""
    app = FastAPI()
    app.include_router(admin_routes.api_router)
    app.include_router(admin_routes.router)

    admin_user = User(
        id="super-x",
        email="super@aumigao.app",
        password_hash="x",
        role="super_admin",
    )
    admin_user._act_as_tenant_id = tenant_id

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin_user
    return TestClient(app)


# ── T1: tutor nascido no tenant conta 1 ───────────────────────────────────────

def test_T1_tutor_nascido_no_tenant_conta():
    db = _make_db()
    _add_tutor(db, uid="u1", email=EMAIL_A, tenant_id=TENANT_A)
    scope = _scope(TENANT_A)
    assert _sql_count_real_tutors(db, scope) == 1


# ── T2: tutor vinculado (nascido em outro tenant) conta 1 ────────────────────

def test_T2_tutor_vinculado_externo_conta():
    db = _make_db()
    # Tutor nasceu no tenant B, mas tem vínculo ativo com tenant A.
    _add_tutor(db, uid="u2", email=EMAIL_B, tenant_id=TENANT_B)
    _add_access(db, tenant_id=TENANT_A, tutor_user_id="u2", status="active")

    scope_a = _scope(TENANT_A)
    scope_b = _scope(TENANT_B)

    # Tenant A: vê o tutor via vínculo.
    assert _sql_count_real_tutors(db, scope_a) == 1
    # Tenant B: vê o tutor pelo tenant_id nascimento.
    assert _sql_count_real_tutors(db, scope_b) == 1


# ── T3: tutor com nascimento + vínculo ativo conta 1 (sem dupla contagem) ────

def test_T3_nascimento_mais_vinculo_conta_1():
    db = _make_db()
    # Tutor nasceu no tenant A e também tem uma linha de TenantTutorAccess no A.
    _add_tutor(db, uid="u3", email=EMAIL_A, tenant_id=TENANT_A)
    _add_access(db, tenant_id=TENANT_A, tutor_user_id="u3", status="active")

    scope = _scope(TENANT_A)
    assert _sql_count_real_tutors(db, scope) == 1


# ── T4: access inativo (pending/revoked) NÃO conta ───────────────────────────

def test_T4_access_inativo_nao_conta():
    db = _make_db()
    # Tutor B com vínculo revogado para tenant A.
    _add_tutor(db, uid="u4", email=EMAIL_B, tenant_id=TENANT_B)
    _add_access(db, tenant_id=TENANT_A, tutor_user_id="u4", status="revoked")
    _add_access(db, tenant_id=TENANT_A, tutor_user_id=str(uuid4()), status="pending")

    scope_a = _scope(TENANT_A)
    # Tenant A não tem tutores nascidos nele e os vínculos estão inativos.
    assert _sql_count_real_tutors(db, scope_a) == 0


# ── T5: listagem GET /admin/tutors inclui tutor vinculado ────────────────────

def test_T5_listagem_inclui_tutor_vinculado():
    db = _make_db()
    # Tutor nascido no tenant B, vinculado a tenant A.
    _add_tutor(db, uid="ext-tutor", email=EMAIL_B, tenant_id=TENANT_B)
    _add_access(db, tenant_id=TENANT_A, tutor_user_id="ext-tutor", status="active")

    client = _client_for(db, tenant_id=TENANT_A)
    resp = client.get("/api/admin/tutors")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert "ext-tutor" in ids


# ── T6: listagem exclui tutor com access revogado ────────────────────────────

def test_T6_listagem_exclui_access_inativo():
    db = _make_db()
    _add_tutor(db, uid="rev-tutor", email=EMAIL_B, tenant_id=TENANT_B)
    _add_access(db, tenant_id=TENANT_A, tutor_user_id="rev-tutor", status="revoked")

    client = _client_for(db, tenant_id=TENANT_A)
    resp = client.get("/api/admin/tutors")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert "rev-tutor" not in ids


# ── T7: escopo global não é afetado (continua correto) ───────────────────────

def test_T7_escopo_global_conta_todos_nascimentos():
    db = _make_db()
    _add_tutor(db, uid="ga", email=EMAIL_A, tenant_id=TENANT_A)
    _add_tutor(db, uid="gb", email=EMAIL_B, tenant_id=TENANT_B)

    scope = _global_scope()
    assert _sql_count_real_tutors(db, scope) == 2


# ── T8: listagem não duplica tutor com nascimento + vínculo ──────────────────

def test_T8_listagem_sem_duplicata():
    db = _make_db()
    _add_tutor(db, uid="dup", email=EMAIL_A, tenant_id=TENANT_A)
    _add_access(db, tenant_id=TENANT_A, tutor_user_id="dup", status="active")

    client = _client_for(db, tenant_id=TENANT_A)
    resp = client.get("/api/admin/tutors")
    assert resp.status_code == 200
    ids = [t["id"] for t in resp.json()]
    assert ids.count("dup") == 1
