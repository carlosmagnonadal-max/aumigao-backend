"""Testes de ROTA (camada HTTP) do grupo FINANCEIRO de app/routes/admin.py.

Padrao do projeto (ver tests/test_routes_walker_quality.py e test_routes_auth.py):
monta um FastAPI MINIMO com apenas o(s) router(s) de admin, SQLite em memoria
(StaticPool), overrides de get_db / get_current_user. NAO importa app.main (que
conecta no Neon de PROD).

Cobre:
- GET/PUT /admin/payment-config (config de comissao/split por tenant; finance.read
  / finance.manage), incl. clamp de comissao 0..100, persistencia e auditoria.
- POST /admin/withdrawals/{id}/approve e /reject (finance.manage): transicao de
  status do Payment para paid/rejected + evento operacional.
- Gating: os routers tem dependency de require_permission("admin.access") e cada
  endpoint exige finance.read/finance.manage. super_admin bypassa o RBAC (rede de
  seguranca em rbac.user_has_permission); role "tutor" falha (403).

Nota: payment-config para super_admin usa scope.tenant_id=None ->
resolve_current_tenant_id -> tenant default (slug "aumigao"). Seedamos esse tenant
para que o config seja criado/lido sob um tenant_id estavel.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.audit_log import AuditLog
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.tenant_payment_config import DEFAULT_COMMISSION_PERCENT, TenantPaymentConfig
from app.models.user import User
from app.routes import admin as admin_routes
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

ADMIN_ID = "admin-1"
TUTOR_ID = "tutor-1"
TENANT_ID = "t-default"


def build(*, current: str = ADMIN_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # tenant default (slug "aumigao") -> resolve_current_tenant_id retorna este id
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    # super_admin -> bypassa admin.access e finance.* no RBAC
    db.add(User(id=ADMIN_ID, email="admin@test.com", password_hash="x", role="super_admin"))
    # tutor comum -> sem admin.access nem finance.* -> 403
    db.add(User(id=TUTOR_ID, email="tutor@test.com", password_hash="x", role="tutor"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin_routes.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current)
    return TestClient(test_app), db


def set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


# ----------------- GET /admin/payment-config -----------------
def test_get_payment_config_happy_path_creates_default():
    client, db = build()
    r = client.get("/admin/payment-config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant_id"] == TENANT_ID
    # Comissão default agora vem do TIER do plano (tenant é "business" -> 8%).
    assert body["commission_percent"] == 8.0
    assert body["commission_is_custom"] is False
    assert body["provider"] == "asaas"
    assert body["split_enabled"] is False
    assert body["active"] is True
    # config foi persistida (get_or_create)
    assert db.query(TenantPaymentConfig).filter(TenantPaymentConfig.tenant_id == TENANT_ID).count() == 1


def test_get_payment_config_exposes_plan_commission_ruler():
    # R9: a régua de comissão por plano vem do backend (não números fixos no front).
    client, db = build()
    r = client.get("/admin/payment-config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plan_commission_defaults"] == {"starter": 12.0, "business": 8.0, "enterprise": 5.0}
    assert body["plan_commission_fallback"] == 10.0


def test_get_payment_config_forbidden_for_non_admin():
    # role "tutor" -> falha na dependency de router require_permission("admin.access")
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.get("/admin/payment-config")
    assert r.status_code == 403


# ----------------- PUT /admin/payment-config -----------------
def test_update_payment_config_persists_and_audits():
    client, db = build()
    r = client.put("/admin/payment-config", json={
        "commission_percent": 30.0,
        "provider": "stripe",
        "split_enabled": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["commission_percent"] == 30.0
    assert body["provider"] == "stripe"
    assert body["split_enabled"] is True
    # persistido
    config = db.query(TenantPaymentConfig).filter(TenantPaymentConfig.tenant_id == TENANT_ID).first()
    assert config.commission_percent == 30.0
    assert config.provider == "stripe"
    assert config.split_enabled is True
    # mudanca de regra financeira gera audit_log
    logs = db.query(AuditLog).filter(AuditLog.action == "payment_config.updated").all()
    assert len(logs) >= 1
    assert logs[-1].entity_id == TENANT_ID


def test_update_payment_config_clamps_commission_above_100():
    client, db = build()
    r = client.put("/admin/payment-config", json={"commission_percent": 150.0})
    assert r.status_code == 200, r.text
    assert r.json()["commission_percent"] == 100.0


def test_update_payment_config_clamps_commission_below_zero():
    client, db = build()
    r = client.put("/admin/payment-config", json={"commission_percent": -10.0})
    assert r.status_code == 200, r.text
    assert r.json()["commission_percent"] == 0.0


def test_update_payment_config_partial_keeps_other_fields():
    client, db = build()
    # seed inicial
    client.put("/admin/payment-config", json={"commission_percent": 25.0, "provider": "asaas"})
    # update parcial so do split
    r = client.put("/admin/payment-config", json={"split_enabled": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["split_enabled"] is True
    # commission e provider preservados
    assert body["commission_percent"] == 25.0
    assert body["provider"] == "asaas"


def test_update_payment_config_blank_provider_is_ignored():
    # provider vazio/somente espacos nao sobrescreve (regra do service)
    client, db = build()
    client.put("/admin/payment-config", json={"provider": "mercadopago"})
    r = client.put("/admin/payment-config", json={"provider": "   "})
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "mercadopago"


def test_update_payment_config_forbidden_for_non_admin():
    client, db = build()
    set_user(client, db, TUTOR_ID)
    r = client.put("/admin/payment-config", json={"commission_percent": 10.0})
    assert r.status_code == 403


# ----------------- POST /admin/withdrawals/{id}/approve -----------------
def _add_payment(db, pid="pay-1", status="pending"):
    db.add(Payment(
        id=pid, tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walk_id="walk-1",
        amount=50.0, status=status, provider="pix",
    ))
    db.commit()


def test_approve_withdrawal_sets_status_paid():
    client, db = build()
    _add_payment(db, pid="pay-approve", status="pending")
    r = client.post("/admin/withdrawals/pay-approve/approve")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    db.expire_all()
    assert db.get(Payment, "pay-approve").status == "paid"


def test_approve_withdrawal_unknown_id_is_noop_ok():
    # endpoint nao levanta 404; apenas ignora se nao existir
    client, db = build()
    r = client.post("/admin/withdrawals/does-not-exist/approve")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}


def test_approve_withdrawal_forbidden_for_non_admin():
    client, db = build()
    _add_payment(db, pid="pay-403", status="pending")
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/withdrawals/pay-403/approve")
    assert r.status_code == 403
    # status inalterado
    db.expire_all()
    assert db.get(Payment, "pay-403").status == "pending"


# ----------------- POST /admin/withdrawals/{id}/reject -----------------
def test_reject_withdrawal_sets_status_rejected():
    client, db = build()
    _add_payment(db, pid="pay-reject", status="pending")
    r = client.post("/admin/withdrawals/pay-reject/reject")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True}
    db.expire_all()
    assert db.get(Payment, "pay-reject").status == "rejected"


def test_reject_withdrawal_forbidden_for_non_admin():
    client, db = build()
    _add_payment(db, pid="pay-rej-403", status="pending")
    set_user(client, db, TUTOR_ID)
    r = client.post("/admin/withdrawals/pay-rej-403/reject")
    assert r.status_code == 403
    db.expire_all()
    assert db.get(Payment, "pay-rej-403").status == "pending"


# ----------------- B-02b: guard — nao-saque nao pode ser aprovado/rejeitado -----------------
def _add_asaas_payment(db, pid="pay-asaas-1", status="pending"):
    """Payment de tutor via Asaas (provider='asaas') — NAO e saque de passeador."""
    db.add(Payment(
        id=pid, tenant_id=TENANT_ID, tutor_id=TUTOR_ID, walk_id="walk-x",
        amount=50.0, status=status, provider="asaas",
    ))
    db.commit()


def test_approve_non_withdrawal_returns_400():
    client, db = build()
    _add_asaas_payment(db, pid="pay-asaas-approve")
    r = client.post("/admin/withdrawals/pay-asaas-approve/approve")
    assert r.status_code == 400, r.text
    db.expire_all()
    # status deve permanecer inalterado
    assert db.get(Payment, "pay-asaas-approve").status == "pending"


def test_reject_non_withdrawal_returns_400():
    client, db = build()
    _add_asaas_payment(db, pid="pay-asaas-reject")
    r = client.post("/admin/withdrawals/pay-asaas-reject/reject")
    assert r.status_code == 400, r.text
    db.expire_all()
    assert db.get(Payment, "pay-asaas-reject").status == "pending"
