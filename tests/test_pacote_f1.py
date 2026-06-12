"""Testes do Pacote F1 — esqueci minha senha, e-mails transacionais, push de pagamento.

Padrão do projeto: FastAPI mínimo + SQLite em memória (StaticPool) + overrides de get_db /
get_current_user. Sem imports de app.main. Sem rede real.

Cobre:
- POST /auth/forgot-password: retorna 200 neutro para e-mail inexistente; rate limit 3×;
  gera código, invalida anteriores.
- POST /auth/reset-password: código errado 5× invalida; código expirado falha; reset troca
  senha (login com nova funciona, antiga falha).
- POST /auth/register dispara welcome email (mock fire-and-forget).
- POST /payments/webhooks/asaas com PAYMENT_CONFIRMED cria notificação 1× só (idempotência).
"""
import os
import time
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.core.security import get_password_hash, verify_password
from app.dependencies.auth import get_current_user
from app.models.notification import Notification
from app.models.password_reset_code import PasswordResetCode
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.routes import auth, payments
from app.services.login_rate_limiter import InMemoryLoginRateLimiter
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-f1-test"
TUTOR_ID = "tutor-f1"
WALKER_ID = "walker-f1"


# --------------------------------------------------------------------------- #
# Helpers de build                                                              #
# --------------------------------------------------------------------------- #

def _engine_and_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def build_auth(*, users: list[dict] | None = None):
    engine, Session = _engine_and_session()
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in users or []:
        db.add(User(**u))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(auth.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


def build_payments(*, users=None, walks=None, existing_payments=None):
    engine, Session = _engine_and_session()
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    for u in users or []:
        db.add(User(**u))
    for w in walks or []:
        db.add(Walk(**w))
    for p in existing_payments or []:
        db.add(Payment(**p))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return TestClient(test_app), db


def make_user(uid=None, email="user@test.com", password="senha1234", **extra):
    base = dict(
        id=uid or str(uuid4()),
        email=email,
        password_hash=get_password_hash(password),
        role="tutor",
        tenant_id=TENANT_ID,
        is_active=True,
        full_name="Test User",
    )
    base.update(extra)
    return base


def make_walk(walk_id=None, tutor_id=TUTOR_ID, **extra):
    base = dict(
        id=walk_id or str(uuid4()),
        tutor_id=tutor_id,
        tenant_id=TENANT_ID,
        pet_id="pet-1",
        scheduled_date="2026-07-01",
        duration_minutes=30,
        price=50.0,
        status="Agendado",
    )
    base.update(extra)
    return base


def make_payment(payment_id=None, walk_id=None, provider_payment_id="asaas-p-1", **extra):
    base = dict(
        id=payment_id or str(uuid4()),
        tenant_id=TENANT_ID,
        tutor_id=TUTOR_ID,
        walk_id=walk_id,
        amount=50.0,
        status="pagamento_sandbox_criado",
        provider="asaas_sandbox",
        provider_payment_id=provider_payment_id,
    )
    base.update(extra)
    return base


# Limpa o rate limiter de forgot-password antes de cada teste
@pytest.fixture(autouse=True)
def _reset_forgot_limiter():
    auth._forgot_password_limiter._failures.clear()
    yield
    auth._forgot_password_limiter._failures.clear()


# --------------------------------------------------------------------------- #
# F1.1 — POST /auth/forgot-password                                            #
# --------------------------------------------------------------------------- #

class TestForgotPassword:
    def test_returns_200_for_nonexistent_email(self):
        client, db = build_auth()
        r = client.post("/auth/forgot-password", json={"email": "naoexiste@test.com"})
        assert r.status_code == 200, r.text
        assert "código" in r.json()["message"].lower() or "cadastrado" in r.json()["message"].lower()

    def test_returns_200_for_existing_email_neutral_message(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="exist@test.com")])
        r = client.post("/auth/forgot-password", json={"email": "exist@test.com"})
        assert r.status_code == 200, r.text
        # Mensagem não revela se existe ou não
        body = r.json()["message"]
        assert "código" in body.lower() or "cadastrado" in body.lower()

    def test_creates_reset_code_for_existing_user(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="exist@test.com")])
        with patch("app.services.transactional_email_service._send_email"):
            r = client.post("/auth/forgot-password", json={"email": "exist@test.com"})
        assert r.status_code == 200
        codes = db.query(PasswordResetCode).filter(PasswordResetCode.user_id == TUTOR_ID).all()
        assert len(codes) == 1
        assert codes[0].used_at is None
        assert codes[0].expires_at > datetime.utcnow()

    def test_invalidates_previous_codes_on_new_request(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="exist@test.com")])
        with patch("app.services.transactional_email_service._send_email"):
            client.post("/auth/forgot-password", json={"email": "exist@test.com"})
            client.post("/auth/forgot-password", json={"email": "exist@test.com"})
        codes = db.query(PasswordResetCode).filter(PasswordResetCode.user_id == TUTOR_ID).all()
        # 2 códigos criados; o primeiro deve estar invalidado (used_at não nulo)
        assert len(codes) == 2
        used_count = sum(1 for c in codes if c.used_at is not None)
        active_count = sum(1 for c in codes if c.used_at is None)
        assert used_count == 1
        assert active_count == 1

    def test_rate_limit_blocks_after_3_attempts(self):
        client, db = build_auth()
        with patch("app.services.transactional_email_service._send_email"):
            for _ in range(3):
                client.post("/auth/forgot-password", json={"email": "naoexiste@test.com"})
        # 4a tentativa deve ser bloqueada
        r = client.post("/auth/forgot-password", json={"email": "naoexiste@test.com"})
        assert r.status_code == 429

    def test_invalid_email_format_returns_200_neutral(self):
        client, db = build_auth()
        r = client.post("/auth/forgot-password", json={"email": "nao-e-email"})
        assert r.status_code == 200


# --------------------------------------------------------------------------- #
# F1.1 — POST /auth/reset-password                                             #
# --------------------------------------------------------------------------- #

def _insert_reset_code(db, user_id: str, code: str, minutes_until_expiry: int = 15, used: bool = False) -> PasswordResetCode:
    from app.routes.auth import _hash_reset_code
    rc = PasswordResetCode(
        id=str(uuid4()),
        user_id=user_id,
        code_hash=_hash_reset_code(code),
        expires_at=datetime.utcnow() + timedelta(minutes=minutes_until_expiry),
        used_at=datetime.utcnow() if used else None,
        attempts=0,
    )
    db.add(rc)
    db.commit()
    return rc


class TestResetPassword:
    def test_happy_path_changes_password(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="reset@test.com", password="senhaAntiga1")])
        _insert_reset_code(db, TUTOR_ID, "123456")
        r = client.post("/auth/reset-password", json={
            "email": "reset@test.com",
            "code": "123456",
            "new_password": "senhaNova9",
        })
        assert r.status_code == 200, r.text
        db.expire_all()
        user = db.query(User).filter(User.email == "reset@test.com").first()
        assert verify_password("senhaNova9", user.password_hash)
        assert not verify_password("senhaAntiga1", user.password_hash)

    def test_old_password_fails_after_reset(self):
        """Após o reset, o login com a senha antiga deve falhar."""
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="reset2@test.com", password="senhaAntiga1")])
        _insert_reset_code(db, TUTOR_ID, "654321")
        client.post("/auth/reset-password", json={
            "email": "reset2@test.com", "code": "654321", "new_password": "senhaNova9",
        })
        # Login com senha antiga deve falhar
        r_login_old = client.post("/auth/login", json={"email": "reset2@test.com", "password": "senhaAntiga1"})
        assert r_login_old.status_code == 401
        # Login com senha nova deve funcionar
        r_login_new = client.post("/auth/login", json={"email": "reset2@test.com", "password": "senhaNova9"})
        assert r_login_new.status_code == 200

    def test_wrong_code_returns_400(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="wrong@test.com")])
        _insert_reset_code(db, TUTOR_ID, "111111")
        r = client.post("/auth/reset-password", json={
            "email": "wrong@test.com", "code": "999999", "new_password": "senhaNova9",
        })
        assert r.status_code == 400

    def test_wrong_code_5_times_invalidates(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="maxattempts@test.com")])
        rc = _insert_reset_code(db, TUTOR_ID, "111111")
        # 5 tentativas erradas
        for _ in range(5):
            client.post("/auth/reset-password", json={
                "email": "maxattempts@test.com", "code": "999999", "new_password": "senhaNova9",
            })
        # Código correto agora deve falhar porque foi invalidado
        r = client.post("/auth/reset-password", json={
            "email": "maxattempts@test.com", "code": "111111", "new_password": "senhaNova9",
        })
        assert r.status_code == 400
        db.expire_all()
        rc_updated = db.get(PasswordResetCode, rc.id)
        assert rc_updated.used_at is not None  # foi invalidado

    def test_expired_code_returns_400(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="expired@test.com")])
        _insert_reset_code(db, TUTOR_ID, "123456", minutes_until_expiry=-1)  # já expirado
        r = client.post("/auth/reset-password", json={
            "email": "expired@test.com", "code": "123456", "new_password": "senhaNova9",
        })
        assert r.status_code == 400

    def test_used_code_returns_400(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="used@test.com")])
        _insert_reset_code(db, TUTOR_ID, "123456", used=True)
        r = client.post("/auth/reset-password", json={
            "email": "used@test.com", "code": "123456", "new_password": "senhaNova9",
        })
        assert r.status_code == 400

    def test_weak_new_password_returns_400(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="weakpw@test.com")])
        _insert_reset_code(db, TUTOR_ID, "123456")
        r = client.post("/auth/reset-password", json={
            "email": "weakpw@test.com", "code": "123456", "new_password": "fraca",
        })
        assert r.status_code == 400
        assert "senha" in r.json()["detail"].lower()

    def test_nonexistent_email_returns_400(self):
        client, db = build_auth()
        r = client.post("/auth/reset-password", json={
            "email": "nao@existe.com", "code": "123456", "new_password": "senhaNova9",
        })
        assert r.status_code == 400

    def test_code_marked_as_used_after_success(self):
        client, db = build_auth(users=[make_user(uid=TUTOR_ID, email="markused@test.com")])
        rc = _insert_reset_code(db, TUTOR_ID, "555555")
        client.post("/auth/reset-password", json={
            "email": "markused@test.com", "code": "555555", "new_password": "senhaNova9",
        })
        db.expire_all()
        rc_updated = db.get(PasswordResetCode, rc.id)
        assert rc_updated.used_at is not None


# --------------------------------------------------------------------------- #
# F1.2 — welcome email no register                                             #
# --------------------------------------------------------------------------- #

class TestWelcomeEmail:
    def test_register_dispatches_welcome_email(self):
        """O register deve chamar send_welcome_email (mock direto na rota de auth)."""
        client, db = build_auth()
        called_with = []

        def _fake_welcome(to, user_name):
            called_with.append((to, user_name))

        with patch("app.routes.auth.send_welcome_email", side_effect=_fake_welcome):
            r = client.post("/auth/register", json={
                "email": "novo@test.com",
                "password": "senha1234",
                "full_name": "Novo Usuário",
                "role": "cliente",
            })
        assert r.status_code == 200, r.text
        # Aguarda a thread daemon terminar (máx 0.5s)
        time.sleep(0.5)
        assert len(called_with) >= 1, f"send_welcome_email não foi chamado. called_with={called_with}"
        assert called_with[0][0] == "novo@test.com"

    def test_welcome_email_failure_does_not_fail_registration(self):
        """Se o envio de e-mail falhar, o registro deve ser completado normalmente."""
        client, db = build_auth()

        def _failing_welcome(to, user_name):
            raise Exception("SMTP down")

        with patch("app.routes.auth.send_welcome_email", side_effect=_failing_welcome):
            r = client.post("/auth/register", json={
                "email": "error@test.com",
                "password": "senha1234",
                "full_name": "Error User",
                "role": "cliente",
            })
        assert r.status_code == 200, r.text
        # Aguarda a thread daemon terminar (máx 2s) — mesmo com exceção, o registro está ok
        import time; time.sleep(0.5)
        assert db.query(User).filter(User.email == "error@test.com").count() == 1


# --------------------------------------------------------------------------- #
# F1.3 — Push quando pagamento é confirmado (webhook Asaas)                    #
# --------------------------------------------------------------------------- #

WEBHOOK_TOKEN = "test-webhook-secret"
WEBHOOK_HEADERS = {"asaas-access-token": WEBHOOK_TOKEN}


@pytest.fixture(autouse=True)
def _set_webhook_token(monkeypatch):
    monkeypatch.setenv("ASAAS_WEBHOOK_TOKEN", WEBHOOK_TOKEN)


class TestPaymentConfirmedWebhook:
    def _build(self, walk_id="walk-1", provider_payment_id="asaas-p-123"):
        users = [make_user(uid=TUTOR_ID, email="tutor@test.com")]
        walk = make_walk(walk_id=walk_id, tutor_id=TUTOR_ID)
        pay = make_payment(walk_id=walk_id, provider_payment_id=provider_payment_id)
        return build_payments(users=users, walks=[walk], existing_payments=[pay])

    def _webhook_body(self, event: str, provider_id: str = "asaas-p-123"):
        return {"event": event, "payment": {"id": provider_id, "status": "CONFIRMED"}}

    def test_payment_confirmed_creates_notification(self):
        client, db = self._build()
        with patch("app.services.push_notifications.send_push_for_notification_background"):
            r = client.post(
                "/payments/webhooks/asaas",
                json=self._webhook_body("PAYMENT_CONFIRMED"),
                headers=WEBHOOK_HEADERS,
            )
        assert r.status_code == 200, r.text
        notifs = db.query(Notification).filter(
            Notification.type == "payment_confirmed",
            Notification.user_id == TUTOR_ID,
        ).all()
        assert len(notifs) == 1
        assert notifs[0].title == "Pagamento confirmado!"

    def test_payment_received_creates_notification(self):
        client, db = self._build(provider_payment_id="asaas-p-recv")
        with patch("app.services.push_notifications.send_push_for_notification_background"):
            r = client.post(
                "/payments/webhooks/asaas",
                json=self._webhook_body("PAYMENT_RECEIVED", "asaas-p-recv"),
                headers=WEBHOOK_HEADERS,
            )
        assert r.status_code == 200, r.text
        notifs = db.query(Notification).filter(
            Notification.type == "payment_confirmed",
            Notification.user_id == TUTOR_ID,
        ).all()
        assert len(notifs) == 1

    def test_repeated_webhook_does_not_duplicate_notification(self):
        """Idempotência: o mesmo evento duas vezes não cria 2 notificações."""
        client, db = self._build()
        with patch("app.services.push_notifications.send_push_for_notification_background"):
            for _ in range(3):
                client.post(
                    "/payments/webhooks/asaas",
                    json=self._webhook_body("PAYMENT_CONFIRMED"),
                    headers=WEBHOOK_HEADERS,
                )
        notifs = db.query(Notification).filter(
            Notification.type == "payment_confirmed",
            Notification.user_id == TUTOR_ID,
        ).all()
        assert len(notifs) == 1, f"Esperado 1 notificação, encontrado {len(notifs)}"

    def test_other_events_do_not_create_payment_confirmed_notification(self):
        client, db = self._build(provider_payment_id="asaas-p-other")
        with patch("app.services.push_notifications.send_push_for_notification_background"):
            r = client.post(
                "/payments/webhooks/asaas",
                json={"event": "PAYMENT_CREATED", "payment": {"id": "asaas-p-other", "status": "PENDING"}},
                headers=WEBHOOK_HEADERS,
            )
        assert r.status_code == 200
        notifs = db.query(Notification).filter(Notification.type == "payment_confirmed").all()
        assert len(notifs) == 0

    def test_unauthorized_webhook_returns_401(self):
        client, db = self._build()
        r = client.post(
            "/payments/webhooks/asaas",
            json=self._webhook_body("PAYMENT_CONFIRMED"),
            headers={"asaas-access-token": "wrong-token"},
        )
        assert r.status_code == 401

    def test_payment_confirmed_updates_status(self):
        client, db = self._build()
        with patch("app.services.push_notifications.send_push_for_notification_background"):
            client.post(
                "/payments/webhooks/asaas",
                json=self._webhook_body("PAYMENT_CONFIRMED"),
                headers=WEBHOOK_HEADERS,
            )
        db.expire_all()
        pay = db.query(Payment).filter(Payment.provider_payment_id == "asaas-p-123").first()
        assert pay.status == "pagamento_confirmado_sandbox"
