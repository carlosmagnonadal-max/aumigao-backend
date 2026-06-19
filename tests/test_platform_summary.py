"""Testes de rota: GET /admin/platform/summary (super_admin-only, is_global).

Padrao do projeto: FastAPI minimo com SQLite in-memory (StaticPool),
override de get_db / get_current_user. NAO importa app.main.

Cobertura:
  (a) super_admin recebe 200 com estrutura esperada.
  (b) admin de tenant recebe 403.
  (c) sem auth (usuario sem admin.access) recebe 401/403.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.tenant_onboarding import TenantOnboarding
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_network_profile import WalkerNetworkProfile
from app.models.walker_profile import WalkerProfile
from app.routes import admin

SUPER_ID = "super-ps"
ADMIN_TENANT_ID = "admin-ps"
TUTOR_ID = "tutor-ps"
TENANT_A = "tenant-ps-a"

SUPER_EMAIL = "super-ps@aumigao.app"
ADMIN_EMAIL = "admin-ps@aumigao.app"
# Email com token fake ("test") -> sem acesso admin.access, gera 403.
TUTOR_EMAIL = "tutor-ps@test.local"


def _build(*, current_id: str = SUPER_ID):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    # super_admin — bypassa RBAC (user_has_permission retorna True para super_admin)
    db.add(User(id=SUPER_ID, email=SUPER_EMAIL, password_hash="x", role="super_admin"))
    # admin de tenant (role="admin"): precisa de RBAC para admin.access — sem seed RBAC
    # tomara 403; exatamente o que queremos testar.
    db.add(User(
        id=ADMIN_TENANT_ID, email=ADMIN_EMAIL, password_hash="x",
        role="admin", tenant_id=TENANT_A,
    ))
    # tutor sem permissao admin.access — token fake no email -> 403
    db.add(User(id=TUTOR_ID, email=TUTOR_EMAIL, password_hash="x", role="tutor"))
    db.commit()

    test_app = FastAPI()
    test_app.include_router(admin.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, current_id)
    return TestClient(test_app), db


def _set_user(client, db, user_id):
    client.app.dependency_overrides[get_current_user] = lambda: db.get(User, user_id)


# ──────────────────────────────────────────────────────────────────── (a) ──
def test_platform_summary_super_admin_200_structure():
    """super_admin recebe 200 com todos os blocos esperados."""
    client, _ = _build(current_id=SUPER_ID)
    r = client.get("/admin/platform/summary")
    assert r.status_code == 200, r.text
    body = r.json()

    # Bloco tenants
    assert "tenants" in body
    t = body["tenants"]
    assert "total" in t
    assert "by_status" in t
    assert "by_plan" in t
    assert "new_last_30d" in t

    # Bloco onboarding
    assert "onboarding" in body
    o = body["onboarding"]
    assert "by_status" in o
    assert "go_live_approved_not_live" in o

    # Bloco platform_revenue
    assert "platform_revenue" in body
    pr = body["platform_revenue"]
    for key in (
        "total_paid_all_time", "total_paid_last_30d",
        "platform_net_all_time", "platform_net_last_30d",
        "payments_with_split", "payments_without_split",
    ):
        assert key in pr, f"faltou platform_revenue.{key}"

    # Bloco walks
    assert "walks" in body
    w = body["walks"]
    for key in ("total", "completed", "in_progress", "scheduled", "critical_recovery"):
        assert key in w, f"faltou walks.{key}"

    # Bloco users
    assert "users" in body
    u = body["users"]
    for key in ("total_tutors", "total_walkers_active", "total_walkers_all"):
        assert key in u, f"faltou users.{key}"

    # Bloco walker_network
    assert "walker_network" in body
    wn = body["walker_network"]
    for key in ("total", "active", "restricted", "enabled"):
        assert key in wn, f"faltou walker_network.{key}"

    # generated_at presente
    assert "generated_at" in body


def test_platform_summary_empty_db_zeroed():
    """Banco vazio => todos os contadores zerados."""
    client, _ = _build(current_id=SUPER_ID)
    r = client.get("/admin/platform/summary")
    assert r.status_code == 200
    body = r.json()

    assert body["tenants"]["total"] == 0
    assert body["tenants"]["new_last_30d"] == 0
    assert body["onboarding"]["go_live_approved_not_live"] == 0
    assert body["platform_revenue"]["total_paid_all_time"] == 0.0
    assert body["platform_revenue"]["payments_with_split"] == 0
    assert body["walks"]["total"] == 0
    assert body["users"]["total_tutors"] == 0
    assert body["users"]["total_walkers_active"] == 0
    assert body["walker_network"]["total"] == 0


def test_platform_summary_counts_tenants():
    """Tenants seeded aparecem nos contadores."""
    client, db = _build(current_id=SUPER_ID)

    db.add(Tenant(id="t1", name="PetA", slug="peta", status="active", plan="starter"))
    db.add(Tenant(id="t2", name="PetB", slug="petb", status="draft", plan="business"))
    db.add(Tenant(
        id="t3", name="PetC", slug="petc", status="active", plan="starter",
        created_at=datetime.utcnow(),
    ))
    db.commit()

    body = client.get("/admin/platform/summary").json()
    t = body["tenants"]
    assert t["total"] == 3
    assert t["by_status"].get("active", 0) == 2
    assert t["by_status"].get("draft", 0) == 1
    assert t["by_plan"].get("starter", 0) == 2
    assert t["by_plan"].get("business", 0) == 1
    # todos foram criados agora (<30d)
    assert t["new_last_30d"] == 3


def test_platform_summary_old_tenant_not_in_new_30d():
    """Tenant criado ha mais de 30 dias nao conta em new_last_30d."""
    client, db = _build(current_id=SUPER_ID)
    old_date = datetime.utcnow() - timedelta(days=60)
    db.add(Tenant(id="t-old", name="Old", slug="old", status="active", plan="starter", created_at=old_date))
    db.commit()

    body = client.get("/admin/platform/summary").json()
    assert body["tenants"]["total"] == 1
    assert body["tenants"]["new_last_30d"] == 0


def test_platform_summary_go_live_approved_not_live():
    """go_live_approved=True mas tenant.status != active conta no alerta."""
    client, db = _build(current_id=SUPER_ID)
    db.add(Tenant(id="t-gl", name="GL", slug="gl", status="draft", plan="starter"))
    db.add(TenantOnboarding(
        id="ob-gl", tenant_id="t-gl",
        onboarding_status="approved", go_live_approved=True,
    ))
    # Tenant ativo com go_live_approved=True: NAO deve contar
    db.add(Tenant(id="t-live", name="Live", slug="live", status="active", plan="starter"))
    db.add(TenantOnboarding(
        id="ob-live", tenant_id="t-live",
        onboarding_status="go_live", go_live_approved=True,
    ))
    db.commit()

    body = client.get("/admin/platform/summary").json()
    assert body["onboarding"]["go_live_approved_not_live"] == 1


def test_platform_summary_revenue_aggregation():
    """Pagamentos pagos somam corretamente; split calculado separa com/sem."""
    client, db = _build(current_id=SUPER_ID)
    # Pago com split
    db.add(Payment(
        id="p1", tenant_id=TENANT_A, tutor_id="u1", amount=100.0,
        status="paid", platform_amount=12.0, walker_amount=88.0,
        provider="asaas",
    ))
    # Pago sem split
    db.add(Payment(
        id="p2", tenant_id=TENANT_A, tutor_id="u1", amount=50.0,
        status="paid", platform_amount=None,
        provider="internal",
    ))
    # Pendente: nao deve contar
    db.add(Payment(
        id="p3", tenant_id=TENANT_A, tutor_id="u1", amount=200.0,
        status="pending",
        provider="asaas",
    ))
    db.commit()

    body = client.get("/admin/platform/summary").json()
    pr = body["platform_revenue"]
    assert pr["total_paid_all_time"] == 150.0
    assert pr["platform_net_all_time"] == 12.0
    assert pr["payments_with_split"] == 1
    assert pr["payments_without_split"] == 1
    # Novos campos: ambos os pagamentos pagos nao tem walk_id => tudo em plans
    assert "gross_revenue_walks" in pr
    assert "gross_revenue_plans" in pr
    assert pr["gross_revenue_walks"] == 0.0
    assert pr["gross_revenue_plans"] == 150.0
    assert pr["gross_revenue_walks"] + pr["gross_revenue_plans"] == pr["total_paid_all_time"]


def test_platform_summary_gross_revenue_breakdown():
    """gross_revenue_walks + gross_revenue_plans == total_paid_all_time."""
    client, db = _build(current_id=SUPER_ID)
    # Pagamento vinculado a um passeio
    db.add(Payment(
        id="pw1", tenant_id=TENANT_A, tutor_id="u1", walk_id="walk-x",
        amount=80.0, status="paid", provider="asaas",
    ))
    # Pagamento de plano (sem passeio)
    db.add(Payment(
        id="pp1", tenant_id=TENANT_A, tutor_id="u1", walk_id=None,
        amount=197.0, status="paid", provider="internal",
    ))
    # Pagamento pendente: nao conta
    db.add(Payment(
        id="pp2", tenant_id=TENANT_A, tutor_id="u1", walk_id=None,
        amount=999.0, status="pending", provider="internal",
    ))
    db.commit()

    body = client.get("/admin/platform/summary").json()
    pr = body["platform_revenue"]
    assert pr["gross_revenue_walks"] == 80.0
    assert pr["gross_revenue_plans"] == 197.0
    assert pr["total_paid_all_time"] == 277.0
    # invariante: walks + plans == total
    assert pr["gross_revenue_walks"] + pr["gross_revenue_plans"] == pr["total_paid_all_time"]


def test_platform_summary_walks_counters():
    """Passeios reais contam por status; walks fake sao ignorados."""
    client, db = _build(current_id=SUPER_ID)
    # walk real completado
    db.add(Walk(
        id="wk-real", tutor_id="tutor-x@aumigao.app", pet_id="pet-1",
        tenant_id=TENANT_A, scheduled_date="2026-06-01 10:00",
        duration_minutes=30, price=40.0, status="ride_completed",
        created_at=datetime.utcnow(),
    ))
    # walk com token fake no tutor_id: deve ser ignorado
    db.add(Walk(
        id="wk-fake", tutor_id="test-fake-tutor", pet_id="pet-2",
        tenant_id=TENANT_A, scheduled_date="2026-06-01 10:00",
        duration_minutes=30, price=40.0, status="ride_completed",
        created_at=datetime.utcnow(),
    ))
    db.commit()

    body = client.get("/admin/platform/summary").json()
    w = body["walks"]
    # walk real: total=1, completed=1
    assert w["total"] == 1
    assert w["completed"] == 1


def test_platform_summary_walker_network():
    """WalkerNetworkProfile agregado corretamente."""
    client, db = _build(current_id=SUPER_ID)
    db.add(User(id="wk1", email="wk1@aumigao.app", password_hash="x", role="walker"))
    db.add(User(id="wk2", email="wk2@aumigao.app", password_hash="x", role="walker"))
    db.add(WalkerNetworkProfile(
        id="np1", walker_user_id="wk1", network_status="active", network_enabled=True,
    ))
    db.add(WalkerNetworkProfile(
        id="np2", walker_user_id="wk2", network_status="restricted", network_enabled=False,
    ))
    db.commit()

    body = client.get("/admin/platform/summary").json()
    wn = body["walker_network"]
    assert wn["total"] == 2
    assert wn["active"] == 1
    assert wn["restricted"] == 1
    assert wn["enabled"] == 1


# ──────────────────────────────────────────────────────────────────── (b) ──
def test_platform_summary_tenant_admin_403():
    """Admin de tenant (nao super_admin) recebe 403."""
    client, db = _build(current_id=SUPER_ID)
    _set_user(client, db, ADMIN_TENANT_ID)
    r = client.get("/admin/platform/summary")
    # admin de tenant sem RBAC seed -> 403 do require_permission
    assert r.status_code == 403


# ──────────────────────────────────────────────────────────────────── (c) ──
def test_platform_summary_no_permission_403():
    """Usuario sem admin.access (tutor) recebe 403."""
    client, db = _build(current_id=SUPER_ID)
    _set_user(client, db, TUTOR_ID)
    r = client.get("/admin/platform/summary")
    assert r.status_code == 403
