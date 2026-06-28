"""Testes de devido processo para suspensao/bloqueio de passeador.

Cobre:
- Suspensao/bloqueio SEM reason -> 422 (guarda-corpos de due process)
- Suspensao/bloqueio COM reason -> audit_log gerado + Notification ao passeador
- Bloqueio imediato ainda funciona (reason presente => status alterado sem espera)
- Fluxos de pagamento nao sao afetados (smoke test de isolamento)

Usa FastAPI TestClient + SQLite in-memory + overrides de get_db/get_current_user.
Padrao identico a tests/test_routes_admin_audit_alerts.py.
"""
import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.audit_log import AuditLog
from app.models.notification import Notification
from app.models.user import User
from app.models.walker_profile import WalkerProfile
from app.routes import admin as admin_mod
from app.routes import partner_application as pa_mod

# ---------------------------------------------------------------------------
# IDs fixos
# ---------------------------------------------------------------------------
SUPER_ID = "super-gov-1"
ADMIN_ID = "admin-gov-1"
WALKER_USER_ID = "walker-user-gov-1"
WALKER_PROFILE_ID = "walker-prof-gov-1"


# ---------------------------------------------------------------------------
# Fixtures de infra
# ---------------------------------------------------------------------------

def _make_engine():
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


def _seed_db(db):
    """Cria super_admin, admin, walker_user e walker_profile no banco de testes."""
    db.add(User(
        id=SUPER_ID,
        email="super-gov@test.com",
        password_hash="x",
        role="super_admin",
    ))
    db.add(User(
        id=ADMIN_ID,
        email="admin-gov@test.com",
        password_hash="x",
        role="admin",
    ))
    db.add(User(
        id=WALKER_USER_ID,
        email="walker-gov@test.com",
        password_hash="x",
        role="walker",
    ))
    db.add(WalkerProfile(
        id=WALKER_PROFILE_ID,
        user_id=WALKER_USER_ID,
        full_name="Passeador Gov Test",
        status="active",
        active_as_walker=True,
    ))
    db.commit()


def _build_admin_client(actor_id: str = SUPER_ID):
    """Monta TestClient com apenas o router admin (padrao audit_alerts)."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    _seed_db(db)

    app = FastAPI()
    app.include_router(admin_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, actor_id)
    client = TestClient(app)
    return client, db


def _build_pa_client(actor_id: str = SUPER_ID):
    """Monta TestClient com o router de partner-applications."""
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    _seed_db(db)

    app = FastAPI()
    app.include_router(pa_mod.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, actor_id)
    client = TestClient(app)
    return client, db


# ===========================================================================
# Grupo 1 — admin.py: PATCH /admin/partner-applications/{id}/admin-fields
# ===========================================================================

class TestAdminPartnerApplicationsAdminFields:
    """PATCH /admin/partner-applications/{id}/admin-fields com status restritivo."""

    def test_block_without_reason_returns_422(self):
        """Status 'blocked' sem reason -> 422 (due process)."""
        client, db = _build_admin_client()
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked"},
        )
        assert r.status_code == 422, r.text
        # Perfil NAO deve ter sido alterado
        db.expire_all()
        profile = db.get(WalkerProfile, WALKER_PROFILE_ID)
        assert profile.status == "active"  # inalterado

    def test_block_with_empty_reason_returns_422(self):
        """Status 'blocked' com reason vazio -> 422."""
        client, db = _build_admin_client()
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": ""},
        )
        assert r.status_code == 422, r.text

    def test_block_with_whitespace_only_reason_returns_422(self):
        """reason com apenas espacos em branco deve ser rejeitado."""
        client, db = _build_admin_client()
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": "   "},
        )
        assert r.status_code == 422, r.text

    def test_suspend_variant_without_reason_returns_422(self):
        """'suspenso' (alias de blocked) sem reason -> 422."""
        client, db = _build_admin_client()
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "suspenso"},
        )
        assert r.status_code == 422, r.text

    def test_block_with_reason_succeeds_immediately(self):
        """Status 'blocked' COM reason valido -> 200, status muda imediatamente."""
        client, db = _build_admin_client()
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": "Fraude confirmada em investigacao interna."},
        )
        assert r.status_code == 200, r.text
        db.expire_all()
        profile = db.get(WalkerProfile, WALKER_PROFILE_ID)
        assert profile.status == "blocked"
        assert profile.active_as_walker is False

    def test_block_generates_audit_log(self):
        """Bloqueio com reason -> audit_log com action walker_profile.status_changed_to_blocked."""
        client, db = _build_admin_client()
        client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": "Comportamento inadequado reportado por tutores."},
        )
        db.expire_all()
        log = (
            db.query(AuditLog)
            .filter(AuditLog.action == "walker_profile.status_changed_to_blocked")
            .first()
        )
        assert log is not None, "Audit log de bloqueio nao foi criado"
        assert log.entity_type == "walker_profile"
        assert log.entity_id == WALKER_PROFILE_ID
        assert log.actor_user_id == SUPER_ID
        after = json.loads(log.after_data)
        assert after["status"] == "blocked"
        assert "Comportamento inadequado" in after["reason"]

    def test_block_generates_notification_to_walker(self):
        """Bloqueio com reason -> Notification ao passeador com motivo e canal de contestacao."""
        client, db = _build_admin_client()
        reason = "Documentos inconsistentes na verificacao de antecedentes."
        client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": reason},
        )
        db.expire_all()
        notif = (
            db.query(Notification)
            .filter(
                Notification.user_id == WALKER_USER_ID,
                Notification.type == "walker_profile_restricted",
            )
            .first()
        )
        assert notif is not None, "Notification ao passeador nao foi criada"
        assert reason in notif.message
        # Canal de contestacao deve aparecer na mensagem
        assert "suporte" in notif.message.lower() or "contestar" in notif.message.lower()

    def test_block_stamps_suspension_columns(self):
        """suspension_reason, status_changed_by e status_changed_at devem ser populados."""
        client, db = _build_admin_client()
        reason = "Acumulou 3 cancelamentos no mesmo dia sem justificativa."
        client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": reason},
        )
        db.expire_all()
        profile = db.get(WalkerProfile, WALKER_PROFILE_ID)
        assert profile.suspension_reason == reason
        assert profile.status_changed_by == SUPER_ID
        assert profile.status_changed_at is not None

    def test_reject_without_reason_returns_422(self):
        """Status 'rejected' sem reason -> 422."""
        client, db = _build_admin_client()
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "rejected"},
        )
        assert r.status_code == 422, r.text

    def test_reject_with_reason_generates_audit_and_notification(self):
        """Reprovacao com reason -> audit_log + notification ao passeador."""
        client, db = _build_admin_client()
        reason = "Documentos de identidade nao coincidem com o CPF cadastrado."
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "rejected", "reason": reason},
        )
        assert r.status_code == 200, r.text
        db.expire_all()
        log = (
            db.query(AuditLog)
            .filter(AuditLog.action == "walker_profile.status_changed_to_rejected")
            .first()
        )
        assert log is not None
        notif = (
            db.query(Notification)
            .filter(Notification.user_id == WALKER_USER_ID)
            .first()
        )
        assert notif is not None
        assert reason in notif.message

    def test_non_restrictive_status_does_not_require_reason(self):
        """Status nao restritivos (ex.: under_review) NAO precisam de reason."""
        client, db = _build_admin_client()
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "under_review"},
        )
        assert r.status_code == 200, r.text
        db.expire_all()
        profile = db.get(WalkerProfile, WALKER_PROFILE_ID)
        assert profile.status == "under_review"


# ===========================================================================
# Grupo 2 — admin.py: POST /admin/walkers/{id}/reject
# ===========================================================================

class TestAdminWalkerReject:
    """POST /admin/walkers/{id}/reject — reason obrigatorio."""

    def test_reject_without_reason_returns_422(self):
        """Rejeitar walker sem payload -> 422."""
        client, db = _build_admin_client()
        r = client.post(f"/admin/walkers/{WALKER_PROFILE_ID}/reject")
        assert r.status_code == 422, r.text

    def test_reject_with_empty_reason_returns_422(self):
        """Rejeitar walker com reason vazio -> 422."""
        client, db = _build_admin_client()
        r = client.post(
            f"/admin/walkers/{WALKER_PROFILE_ID}/reject",
            json={"reason": ""},
        )
        assert r.status_code == 422, r.text

    def test_reject_with_reason_changes_status_immediately(self):
        """Rejeitar walker COM reason -> 200, status == rejected imediatamente."""
        client, db = _build_admin_client()
        r = client.post(
            f"/admin/walkers/{WALKER_PROFILE_ID}/reject",
            json={"reason": "Walker nao compareceu a verificacao presencial marcada."},
        )
        assert r.status_code == 200, r.text
        db.expire_all()
        profile = db.get(WalkerProfile, WALKER_PROFILE_ID)
        assert profile.status == "rejected"

    def test_reject_generates_audit_log(self):
        """Rejeicao com reason -> audit_log registrado."""
        client, db = _build_admin_client()
        reason = "Multiplas reclamacoes de tutores em menos de 30 dias."
        client.post(
            f"/admin/walkers/{WALKER_PROFILE_ID}/reject",
            json={"reason": reason},
        )
        db.expire_all()
        log = (
            db.query(AuditLog)
            .filter(AuditLog.action == "walker_profile.status_changed_to_rejected")
            .first()
        )
        assert log is not None, "Audit log de rejeicao nao foi criado"
        assert log.actor_user_id == SUPER_ID
        assert log.entity_id == WALKER_PROFILE_ID

    def test_reject_generates_notification_with_reason_and_contestation_channel(self):
        """Rejeicao com reason -> Notification com motivo e canal de contestacao."""
        client, db = _build_admin_client()
        reason = "Foto de perfil nao mostra rosto claramente."
        client.post(
            f"/admin/walkers/{WALKER_PROFILE_ID}/reject",
            json={"reason": reason},
        )
        db.expire_all()
        notif = (
            db.query(Notification)
            .filter(
                Notification.user_id == WALKER_USER_ID,
                Notification.type == "walker_profile_restricted",
            )
            .first()
        )
        assert notif is not None, "Notification nao foi criada"
        assert reason in notif.message
        # Contestacao: suporte ou e-mail deve aparecer
        assert "suporte" in notif.message.lower() or "contestar" in notif.message.lower()


# ===========================================================================
# Grupo 3 — partner_application.py: PATCH /{id}/status
# ===========================================================================

class TestPartnerApplicationStatus:
    """PATCH /api/partner-applications/{id}/status com status restritivo."""

    def test_blocked_without_reason_returns_422(self):
        """Status blocked via partner-applications/status sem reason -> 422."""
        client, db = _build_pa_client()
        r = client.patch(
            f"/api/partner-applications/{WALKER_PROFILE_ID}/status",
            json={"status": "blocked"},
        )
        assert r.status_code == 422, r.text

    def test_blocked_with_reason_returns_200_and_generates_audit(self):
        """Status blocked com reason -> 200 + audit_log."""
        client, db = _build_pa_client()
        reason = "Fraude detectada pelo sistema automatico de monitoramento."
        r = client.patch(
            f"/api/partner-applications/{WALKER_PROFILE_ID}/status",
            json={"status": "blocked", "reason": reason},
        )
        assert r.status_code == 200, r.text
        db.expire_all()
        profile = db.get(WalkerProfile, WALKER_PROFILE_ID)
        assert profile.status == "blocked"
        log = (
            db.query(AuditLog)
            .filter(AuditLog.action == "walker_profile.status_changed_to_blocked")
            .first()
        )
        assert log is not None

    def test_non_restrictive_status_allowed_without_reason(self):
        """Status nao restritivo (approved) nao exige reason."""
        client, db = _build_pa_client()
        # Ajusta para approved para evitar colisao com restricao de active_as_walker
        profile = db.get(WalkerProfile, WALKER_PROFILE_ID)
        profile.status = "submitted"
        db.commit()
        r = client.patch(
            f"/api/partner-applications/{WALKER_PROFILE_ID}/status",
            json={"status": "approved"},
        )
        assert r.status_code == 200, r.text


# ===========================================================================
# Grupo 4 — partner_application.py: PATCH /{id}/admin-fields
# ===========================================================================

class TestPartnerApplicationAdminFields:
    """PATCH /api/partner-applications/{id}/admin-fields com status restritivo."""

    def test_blocked_without_reason_returns_422(self):
        client, db = _build_pa_client()
        r = client.patch(
            f"/api/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked"},
        )
        assert r.status_code == 422, r.text

    def test_blocked_with_reason_returns_200_and_notifies_walker(self):
        client, db = _build_pa_client()
        reason = "Reclamacao grave de abandono de animal durante passeio."
        r = client.patch(
            f"/api/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": reason},
        )
        assert r.status_code == 200, r.text
        db.expire_all()
        notif = (
            db.query(Notification)
            .filter(
                Notification.user_id == WALKER_USER_ID,
                Notification.type == "walker_profile_restricted",
            )
            .first()
        )
        assert notif is not None
        assert reason in notif.message


# ===========================================================================
# Grupo 5 — Isolamento: pagamento e matching nao sao afetados
# ===========================================================================

class TestPaymentIsolation:
    """Bloqueio imediato nao altera logica de pagamento (smoke test)."""

    def test_block_does_not_change_walk_payment_state(self):
        """Apos bloqueio, nao ha efeito colateral em pagamentos (tabela nao tocada)."""
        from app.models.payment import Payment

        client, db = _build_admin_client()
        # Nao ha payments no banco de teste — apenas verificamos que o endpoint
        # de bloqueio nao lanca excecao e nao toca a tabela de pagamentos.
        r = client.patch(
            f"/admin/partner-applications/{WALKER_PROFILE_ID}/admin-fields",
            json={"status": "blocked", "reason": "Teste de isolamento de pagamento."},
        )
        assert r.status_code == 200, r.text
        # Tabela de payments deve estar vazia (nao foi tocada).
        payment_count = db.query(Payment).count()
        assert payment_count == 0
