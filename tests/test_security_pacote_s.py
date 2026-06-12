"""Testes do Pacote S — Segurança e Integridade.

Cobre:
(a) Os 4 endpoints admin retornam 401/403 sem token valido:
    - GET /admin/operational-alerts
    - GET /admin/walkers
    - GET /admin/partner-applications
    - GET /admin/walker-operations

(b) Idempotencia de pagamento: duas chamadas com o mesmo walk_id
    resultam em apenas 1 Payment no banco (a segunda retorna o existente).

(c) Referral marcado na mesma transacao da aprovacao/rejeicao de walker
    (S5): mark_referral_approved/rejected chamados com commit=False antes
    do db.commit() principal.

Padrao do projeto: FastAPI minimo com SQLite em memoria, overrides de
get_db / get_current_user, sem importar app.main.
"""
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_referral import WalkerReferral
from app.routes import admin, payments
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG
from app.services.walker_referrals import mark_referral_approved, mark_referral_rejected

TENANT_ID = "t-sec"
ADMIN_ID = "admin-sec"
TUTOR_ID = "tutor-sec"
CAND_ID = "cand-sec"
CAND_USER_ID = "cand-user-sec"


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------

def _admin_engine_and_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Seg", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    # super_admin: passa em todos os checks RBAC (atalho de seguranca)
    db.add(User(id=ADMIN_ID, email="adm@seg.com", password_hash="x", role="super_admin", tenant_id=TENANT_ID))
    db.add(User(id=TUTOR_ID, email="tutor@seg.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=CAND_USER_ID, email="cand@seg.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(WalkerProfile(
        id=CAND_ID, user_id=CAND_USER_ID, full_name="Carlos Passeador",
        cpf="52998224725", phone="11987654321", city="Sao Paulo", state="SP",
        status="under_review", created_at=datetime.utcnow(),
    ))
    db.commit()
    return engine, db


def _build_admin_app(db, current_user_id: str | None = None):
    """Monta app minimo com o router de admin."""
    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if current_user_id is not None:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current_user_id)
    # Sem override de get_current_user: comportamento real (HTTPBearer -> 401)
    return TestClient(test_app)


def _build_payment_app(db, current_user_id: str | None = None):
    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    if current_user_id is not None:
        test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current_user_id)
    return TestClient(test_app)


def _payment_engine_and_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Seg", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor@seg.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    return engine, db


def _fake_asaas_ok(provider_id="asaas-pay-s1", status="PENDING"):
    async def _coro(payload, user):
        return {"id": provider_id, "status": status, "invoiceUrl": None, "bankSlipUrl": None}, {}, "PIX"
    return _coro


# ---------------------------------------------------------------------------
# (a) S1 — Endpoints admin retornam 401/403 sem autenticacao
# ---------------------------------------------------------------------------

class TestS1AdminEndpointsRequireAuth:
    """Os 4 endpoints devem ser protegidos — sem token valido retornam 401."""

    def test_operational_alerts_requires_auth(self):
        _, db = _admin_engine_and_db()
        # Sem override de get_current_user -> HTTPBearer falha -> 401
        client = _build_admin_app(db, current_user_id=None)
        r = client.get("/admin/operational-alerts")
        assert r.status_code == 401, r.text

    def test_walkers_requires_auth(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=None)
        r = client.get("/admin/walkers")
        assert r.status_code == 401, r.text

    def test_partner_applications_requires_auth(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=None)
        r = client.get("/admin/partner-applications")
        assert r.status_code == 401, r.text

    def test_walker_operations_requires_auth(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=None)
        r = client.get("/admin/walker-operations")
        assert r.status_code == 401, r.text

    def test_operational_alerts_403_for_tutor(self):
        _, db = _admin_engine_and_db()
        # tutor autenticado mas sem permissao admin.access -> 403
        client = _build_admin_app(db, current_user_id=TUTOR_ID)
        r = client.get("/admin/operational-alerts")
        assert r.status_code == 403, r.text

    def test_walkers_403_for_tutor(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=TUTOR_ID)
        r = client.get("/admin/walkers")
        assert r.status_code == 403, r.text

    def test_partner_applications_403_for_tutor(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=TUTOR_ID)
        r = client.get("/admin/partner-applications")
        assert r.status_code == 403, r.text

    def test_walker_operations_403_for_tutor(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=TUTOR_ID)
        r = client.get("/admin/walker-operations")
        assert r.status_code == 403, r.text

    def test_operational_alerts_200_for_super_admin(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=ADMIN_ID)
        r = client.get("/admin/operational-alerts")
        assert r.status_code == 200, r.text

    def test_walkers_200_for_super_admin(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=ADMIN_ID)
        r = client.get("/admin/walkers")
        assert r.status_code == 200, r.text

    def test_partner_applications_200_for_super_admin(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=ADMIN_ID)
        r = client.get("/admin/partner-applications")
        assert r.status_code == 200, r.text

    def test_walker_operations_200_for_super_admin(self):
        _, db = _admin_engine_and_db()
        client = _build_admin_app(db, current_user_id=ADMIN_ID)
        r = client.get("/admin/walker-operations")
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# (b) S3 — Idempotencia de pagamento (2 chamadas -> 1 payment)
# ---------------------------------------------------------------------------

class TestS3PaymentIdempotency:

    @pytest.fixture(autouse=True)
    def _force_sandbox(self, monkeypatch):
        monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")

    def test_two_calls_same_walk_id_return_same_payment(self, monkeypatch):
        """Segunda chamada com mesmo walk_id retorna o payment existente sem criar novo."""
        monkeypatch.setattr(payments, "create_asaas_payment", _fake_asaas_ok())
        _, db = _payment_engine_and_db()
        client = _build_payment_app(db, current_user_id=TUTOR_ID)

        r1 = client.post("/payments/create", json={"amount": 50.0, "walk_id": "walk-idem-1", "method": "pix"})
        assert r1.status_code == 200, r1.text
        id1 = r1.json()["id"]

        # Segunda chamada: Asaas nao deve ser chamado de novo (se fosse, criaria outro)
        r2 = client.post("/payments/create", json={"amount": 50.0, "walk_id": "walk-idem-1", "method": "pix"})
        assert r2.status_code == 200, r2.text
        id2 = r2.json()["id"]

        # Mesmo ID retornado
        assert id1 == id2, f"Esperava idempotencia mas obteve ids diferentes: {id1} != {id2}"
        # Apenas 1 registro no banco
        assert db.query(Payment).filter(Payment.walk_id == "walk-idem-1").count() == 1

    def test_different_walk_ids_create_separate_payments(self, monkeypatch):
        """walk_ids diferentes devem gerar payments independentes."""
        monkeypatch.setattr(payments, "create_asaas_payment", _fake_asaas_ok())
        _, db = _payment_engine_and_db()
        client = _build_payment_app(db, current_user_id=TUTOR_ID)

        r1 = client.post("/payments/create", json={"amount": 30.0, "walk_id": "walk-a", "method": "pix"})
        r2 = client.post("/payments/create", json={"amount": 30.0, "walk_id": "walk-b", "method": "pix"})
        assert r1.status_code == 200 and r2.status_code == 200

        assert r1.json()["id"] != r2.json()["id"]
        assert db.query(Payment).count() == 2

    def test_no_walk_id_always_creates_new_payment(self, monkeypatch):
        """Sem walk_id, nao ha idempotencia e cada chamada cria um novo payment."""
        monkeypatch.setattr(payments, "create_asaas_payment", _fake_asaas_ok())
        _, db = _payment_engine_and_db()
        client = _build_payment_app(db, current_user_id=TUTOR_ID)

        client.post("/payments/create", json={"amount": 20.0, "method": "pix"})
        client.post("/payments/create", json={"amount": 20.0, "method": "pix"})
        assert db.query(Payment).count() == 2

    def test_finalized_payment_does_not_block_new_one(self, monkeypatch):
        """Payment com status final nao bloqueia criacao de novo payment para o mesmo walk."""
        monkeypatch.setattr(payments, "create_asaas_payment", _fake_asaas_ok())
        _, db = _payment_engine_and_db()

        # Cria payment ja finalizado (status nao esta em PAYMENT_PENDING_STATUSES)
        db.add(Payment(
            id="pay-final", tenant_id=TENANT_ID, tutor_id=TUTOR_ID,
            walk_id="walk-finalizado", amount=50.0,
            status="pagamento_confirmado_sandbox",  # status final
            provider="asaas_sandbox",
        ))
        db.commit()

        client = _build_payment_app(db, current_user_id=TUTOR_ID)
        r = client.post("/payments/create", json={"amount": 50.0, "walk_id": "walk-finalizado", "method": "pix"})
        assert r.status_code == 200, r.text
        # Deve criar novo payment (o existente esta finalizado, nao pendente)
        assert db.query(Payment).filter(Payment.walk_id == "walk-finalizado").count() == 2


# ---------------------------------------------------------------------------
# (c) S5 — Referral marcado na mesma transacao da aprovacao de walker
# ---------------------------------------------------------------------------

class TestS5ReferralAtomicApproval:
    """Verifica que mark_referral_approved/rejected participa da mesma transacao."""

    def _build_db_with_referral(self, referral_status: str = "under_review"):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        db.add(User(id=CAND_USER_ID, email="cand@seg.com", password_hash="x", role="cliente"))
        referral = WalkerReferral(
            id="ref-1",
            referrer_user_id="outro-user",
            referred_user_id=CAND_USER_ID,
            referred_name="Carlos",
            referred_phone="11999999999",
            referred_phone_normalized="11999999999",
            city="SP",
            neighborhood="Centro",
            referral_code="AUM-ABCDEF-123456",
            invite_link="/walker/register?referralCode=AUM-ABCDEF-123456",
            status=referral_status,
            reward_status="not_eligible",
            performance_status="neutral",
        )
        db.add(referral)
        db.commit()
        return db

    def test_mark_approved_commit_false_does_not_commit_early(self):
        """commit=False nao persiste isoladamente — deve precisar de commit externo."""
        db = self._build_db_with_referral("under_review")
        mark_referral_approved(CAND_USER_ID, db, commit=False)

        # Abre nova sessao para verificar: sem o commit externo, estado nao deve persistir
        # (SQLite StaticPool compartilha conexao, entao a sessao vê as mudancas nao commitadas
        # dentro da mesma conexao — verificamos via rollback)
        db.rollback()
        ref = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == CAND_USER_ID).first()
        # Apos rollback, o status deve ter voltado ao original
        assert ref.status == "under_review"

    def test_mark_approved_with_commit_true_persists(self):
        """commit=True (default) persiste imediatamente — comportamento original."""
        db = self._build_db_with_referral("under_review")
        mark_referral_approved(CAND_USER_ID, db)  # commit=True por default

        db.expire_all()
        ref = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == CAND_USER_ID).first()
        assert ref.status == "approved"
        assert ref.reward_status == "pending"

    def test_mark_rejected_commit_false_does_not_commit_early(self):
        """commit=False para rejected nao persiste sem commit externo."""
        db = self._build_db_with_referral("under_review")
        mark_referral_rejected(CAND_USER_ID, "motivo", db, commit=False)

        db.rollback()
        ref = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == CAND_USER_ID).first()
        assert ref.status == "under_review"

    def test_mark_rejected_with_commit_true_persists(self):
        """commit=True (default) para rejected persiste imediatamente."""
        db = self._build_db_with_referral("under_review")
        mark_referral_rejected(CAND_USER_ID, "motivo de rejeicao", db)

        db.expire_all()
        ref = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == CAND_USER_ID).first()
        assert ref.status == "rejected"
        assert ref.reward_status == "cancelled"
        assert ref.rejection_reason == "motivo de rejeicao"

    def test_approve_and_profile_in_same_transaction(self):
        """Aprovacao de perfil e mark_referral_approved devem ser atomicos."""
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        db.add(User(id=CAND_USER_ID, email="c@seg.com", password_hash="x", role="cliente"))
        profile = WalkerProfile(
            id=CAND_ID, user_id=CAND_USER_ID, full_name="Carlos Passeador",
            cpf="52998224725", phone="11987654321", city="SP", state="SP",
            status="under_review", created_at=datetime.utcnow(),
        )
        referral = WalkerReferral(
            id="ref-2",
            referrer_user_id="outro",
            referred_user_id=CAND_USER_ID,
            referred_name="Carlos",
            referred_phone="11999999999",
            referred_phone_normalized="11999999999",
            city="SP",
            neighborhood="Centro",
            referral_code="AUM-XYZ-789",
            invite_link="/walker/register?referralCode=AUM-XYZ-789",
            status="under_review",
            reward_status="not_eligible",
            performance_status="neutral",
        )
        db.add(profile)
        db.add(referral)
        db.commit()

        # Simula o que o endpoint faz: altera perfil, chama mark com commit=False,
        # depois faz o commit principal.
        profile.status = "active"
        mark_referral_approved(CAND_USER_ID, db, commit=False)
        db.commit()

        db.expire_all()
        updated_ref = db.query(WalkerReferral).filter(WalkerReferral.referred_user_id == CAND_USER_ID).first()
        updated_profile = db.get(WalkerProfile, CAND_ID)
        assert updated_profile.status == "active"
        assert updated_ref.status == "approved"
        assert updated_ref.reward_status == "pending"
