"""Testes de isolamento de tenant nos 8 endpoints admin de ESCRITA (Task 1).

Endpoints cobertos:
- PATCH /admin/walks/{walk_id}/status
- POST  /admin/walks/{walk_id}/recovery
- POST  /admin/walk-completions/{review_id}/approve
- POST  /admin/walk-completions/{review_id}/reject
- PATCH /admin/partner-applications/{candidate_id}/admin-fields
- POST  /admin/walkers/{walker_id}/approve
- POST  /admin/walkers/{walker_id}/reject
- PATCH /admin/partner-applications/{candidate_id}/background-certificate/{cert_id}

Para cada endpoint:
  1. Admin do tenant A recebe 404 ao operar sobre entidade do tenant B.
  2. Admin do tenant A tem sucesso ao operar sobre entidade do seu próprio tenant.
  3. super_admin (escopo global) tem sucesso cross-tenant.

Padrão: FastAPI mínimo + SQLite em memória (StaticPool) + overrides de get_db /
get_current_user.

Para simular admin de tenant com RBAC funcional, usamos super_admin com
_act_as_tenant_id setado — o mesmo mecanismo de produção (X-Act-As-Tenant).
Isso evita a necessidade de semear toda a hierarquia de Role/Permission/
UserRoleAssignment para cada teste, enquanto exercita exatamente o mesmo
caminho de código que um admin de tenant percorre em produção.
"""
from datetime import datetime

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walker_profile import WalkerProfile
from app.models.walker_background_certificate import WalkerBackgroundCertificate
from app.routes import admin as admin_module

# ---------------------------------------------------------------------------
# IDs fixos para os dois tenants
# ---------------------------------------------------------------------------
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

# super_admin que opera "como tenant A" — usa _act_as_tenant_id = TENANT_A.
# Quando _act_as_tenant_id for None, o mesmo usuário opera como global (cross-tenant).
SUPER_AS_A_ID = "super-as-a"
SUPER_GLOBAL_ID = "super-global"

TUTOR_A_ID = "tutor-a"
TUTOR_B_ID = "tutor-b"

WALKER_USER_A_ID = "walker-user-a"
WALKER_USER_B_ID = "walker-user-b"

WALKER_PROFILE_A_ID = "walker-profile-a"
WALKER_PROFILE_B_ID = "walker-profile-b"

WALK_A_ID = "walk-a"
WALK_B_ID = "walk-b"

REVIEW_A_ID = "review-a"
REVIEW_B_ID = "review-b"

CERT_A_ID = "cert-a"
CERT_B_ID = "cert-b"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    _seed_db(session)
    yield session
    session.close()


def _seed_db(db):
    """Cria 2 tenants com walker + walk + review + cert cada."""
    # super_admin que pode operar como tenant A (via _act_as_tenant_id)
    db.add(User(id=SUPER_AS_A_ID, email="super-a@t.com", password_hash="x",
                full_name="Super as A", role="super_admin", tenant_id=None))
    # super_admin global (sem _act_as_tenant_id — vê tudo)
    db.add(User(id=SUPER_GLOBAL_ID, email="super-g@t.com", password_hash="x",
                full_name="Super Global", role="super_admin", tenant_id=None))

    # Tutores
    db.add(User(id=TUTOR_A_ID, email="tutor-a@t.com", password_hash="x",
                full_name="Tutor A", role="tutor", tenant_id=TENANT_A))
    db.add(User(id=TUTOR_B_ID, email="tutor-b@t.com", password_hash="x",
                full_name="Tutor B", role="tutor", tenant_id=TENANT_B))

    # Walker users (o tenant_id no User é o que determina o scope-check p/ walkers)
    db.add(User(id=WALKER_USER_A_ID, email="walker-a@t.com", password_hash="x",
                full_name="Walker A", role="walker", tenant_id=TENANT_A))
    db.add(User(id=WALKER_USER_B_ID, email="walker-b@t.com", password_hash="x",
                full_name="Walker B", role="walker", tenant_id=TENANT_B))

    # WalkerProfiles (sem tenant_id — walkers são globais)
    db.add(WalkerProfile(id=WALKER_PROFILE_A_ID, user_id=WALKER_USER_A_ID,
                         full_name="Walker A", cpf="", phone="", birth_date="",
                         city="", state="", experience="", bio="", rg="",
                         status="submitted"))
    db.add(WalkerProfile(id=WALKER_PROFILE_B_ID, user_id=WALKER_USER_B_ID,
                         full_name="Walker B", cpf="", phone="", birth_date="",
                         city="", state="", experience="", bio="", rg="",
                         status="submitted"))

    # WalkerBackgroundCertificates
    db.add(WalkerBackgroundCertificate(
        id=CERT_A_ID, walker_profile_id=WALKER_PROFILE_A_ID,
        cert_type="pf", status="pending"))
    db.add(WalkerBackgroundCertificate(
        id=CERT_B_ID, walker_profile_id=WALKER_PROFILE_B_ID,
        cert_type="pf", status="pending"))

    # Walks
    db.add(Walk(
        id=WALK_A_ID, tutor_id=TUTOR_A_ID, tenant_id=TENANT_A,
        pet_id="pet-a", scheduled_date="2026-06-01T10:00:00",
        duration_minutes=30, price=50.0, status="Em andamento",
        operational_status="ride_in_progress",
    ))
    db.add(Walk(
        id=WALK_B_ID, tutor_id=TUTOR_B_ID, tenant_id=TENANT_B,
        pet_id="pet-b", scheduled_date="2026-06-01T10:00:00",
        duration_minutes=30, price=50.0, status="Em andamento",
        operational_status="ride_in_progress",
    ))

    # WalkCompletionReviews (com tenant_id para scope-check direto)
    db.add(WalkCompletionReview(
        id=REVIEW_A_ID, walk_id=WALK_A_ID, tenant_id=TENANT_A,
        walker_user_id=WALKER_USER_A_ID, tutor_user_id=TUTOR_A_ID,
        status="pending_review", photo_url="https://x.com/a.jpg", notes="ok",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))
    db.add(WalkCompletionReview(
        id=REVIEW_B_ID, walk_id=WALK_B_ID, tenant_id=TENANT_B,
        walker_user_id=WALKER_USER_B_ID, tutor_user_id=TUTOR_B_ID,
        status="pending_review", photo_url="https://x.com/b.jpg", notes="ok",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))

    db.commit()


def _make_client(db_session, user_id: str, act_as_tenant: str | None = None) -> TestClient:
    """Monta um TestClient com o usuário dado.

    act_as_tenant: quando fornecido, seta _act_as_tenant_id no user (simula
    um super_admin operando como tenant específico, exatamente como em produção
    via X-Act-As-Tenant). None = escopo global.
    """
    test_app = FastAPI()
    test_app.include_router(admin_module.router)
    test_app.dependency_overrides[get_db] = lambda: db_session

    def _current_user() -> User:
        user = db_session.get(User, user_id)
        user._act_as_tenant_id = act_as_tenant
        return user

    test_app.dependency_overrides[get_current_user] = _current_user
    return TestClient(test_app)


# ---------------------------------------------------------------------------
# PATCH /admin/walks/{walk_id}/status
# ---------------------------------------------------------------------------

class TestWalkStatusTenantIsolation:
    def test_tenant_a_admin_cannot_update_walk_b_status(self, db_session):
        """Admin operando como tenant A recebe 404 ao tentar alterar walk do tenant B."""
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.patch(f"/admin/walks/{WALK_B_ID}/status", json={"status": "ride_cancelled"})
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_update_own_walk_status(self, db_session):
        """Admin operando como tenant A consegue alterar walk do próprio tenant."""
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.patch(f"/admin/walks/{WALK_A_ID}/status", json={"status": "ride_cancelled"})
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_update_walk_b_status(self, db_session):
        """super_admin com escopo global consegue alterar walk de qualquer tenant."""
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.patch(f"/admin/walks/{WALK_B_ID}/status", json={"status": "ride_cancelled"})
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# POST /admin/walks/{walk_id}/recovery
# ---------------------------------------------------------------------------

class TestWalkRecoveryTenantIsolation:
    def test_tenant_a_admin_cannot_recover_walk_b(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walks/{WALK_B_ID}/recovery")
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_recover_own_walk(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walks/{WALK_A_ID}/recovery")
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_recover_walk_b(self, db_session):
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.post(f"/admin/walks/{WALK_B_ID}/recovery")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# POST /admin/walk-completions/{review_id}/approve
# ---------------------------------------------------------------------------

class TestWalkCompletionApproveTenantIsolation:
    def test_tenant_a_admin_cannot_approve_review_b(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walk-completions/{REVIEW_B_ID}/approve")
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_approve_own_review(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walk-completions/{REVIEW_A_ID}/approve")
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_approve_review_b(self, db_session):
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.post(f"/admin/walk-completions/{REVIEW_B_ID}/approve")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# POST /admin/walk-completions/{review_id}/reject
# ---------------------------------------------------------------------------

class TestWalkCompletionRejectTenantIsolation:
    def test_tenant_a_admin_cannot_reject_review_b(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walk-completions/{REVIEW_B_ID}/reject")
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_reject_own_review(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walk-completions/{REVIEW_A_ID}/reject")
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_reject_review_b(self, db_session):
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.post(f"/admin/walk-completions/{REVIEW_B_ID}/reject")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# PATCH /admin/partner-applications/{candidate_id}/admin-fields
# ---------------------------------------------------------------------------

class TestPartnerApplicationAdminFieldsTenantIsolation:
    def test_tenant_a_admin_cannot_update_walker_b_admin_fields(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_B_ID}/admin-fields",
            json={"internal_notes": "tentativa cruzada"},
        )
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_update_own_walker_admin_fields(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_A_ID}/admin-fields",
            json={"internal_notes": "nota válida"},
        )
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_update_walker_b_admin_fields(self, db_session):
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_B_ID}/admin-fields",
            json={"internal_notes": "super admin atualiza"},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# POST /admin/walkers/{walker_id}/approve
# ---------------------------------------------------------------------------

class TestWalkerApproveTenantIsolation:
    def test_tenant_a_admin_cannot_approve_walker_b(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walkers/{WALKER_PROFILE_B_ID}/approve")
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_approve_own_walker(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walkers/{WALKER_PROFILE_A_ID}/approve")
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_approve_walker_b(self, db_session):
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.post(f"/admin/walkers/{WALKER_PROFILE_B_ID}/approve")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# POST /admin/walkers/{walker_id}/reject
# ---------------------------------------------------------------------------

class TestWalkerRejectTenantIsolation:
    def test_tenant_a_admin_cannot_reject_walker_b(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(f"/admin/walkers/{WALKER_PROFILE_B_ID}/reject")
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_reject_own_walker(self, db_session):
        # Devido processo: reason obrigatorio para rejeicao (status restritivo).
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.post(
            f"/admin/walkers/{WALKER_PROFILE_A_ID}/reject",
            json={"reason": "Documentos invalidos apos verificacao presencial."},
        )
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_reject_walker_b(self, db_session):
        # Devido processo: reason obrigatorio para rejeicao (status restritivo).
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.post(
            f"/admin/walkers/{WALKER_PROFILE_B_ID}/reject",
            json={"reason": "Perfil duplicado detectado pelo sistema."},
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# PATCH /admin/partner-applications/{candidate_id}/background-certificate/{cert_id}
# ---------------------------------------------------------------------------

class TestBackgroundCertificateTenantIsolation:
    def test_tenant_a_admin_cannot_validate_cert_of_walker_b(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_B_ID}"
            f"/background-certificate/{CERT_B_ID}",
            json={"status": "validated"},
        )
        assert r.status_code == 404, r.text

    def test_tenant_a_admin_can_validate_own_walker_cert(self, db_session):
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_A_ID}"
            f"/background-certificate/{CERT_A_ID}",
            json={"status": "validated"},
        )
        assert r.status_code == 200, r.text

    def test_super_admin_global_can_validate_cert_of_walker_b(self, db_session):
        client = _make_client(db_session, SUPER_GLOBAL_ID, act_as_tenant=None)
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_B_ID}"
            f"/background-certificate/{CERT_B_ID}",
            json={"status": "validated"},
        )
        assert r.status_code == 200, r.text

    def test_tenant_a_admin_cannot_access_cert_via_cross_profile(self, db_session):
        """Admin A tentando usar profile B: scope check bloqueia no profile B antes do cert."""
        client = _make_client(db_session, SUPER_AS_A_ID, act_as_tenant=TENANT_A)
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_B_ID}"
            f"/background-certificate/{CERT_A_ID}",
            json={"status": "validated"},
        )
        # 404 pelo scope check do perfil (walker B não pertence ao tenant A)
        assert r.status_code == 404, r.text
