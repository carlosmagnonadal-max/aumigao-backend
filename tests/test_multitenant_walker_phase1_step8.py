"""Testes — Fase 1 Passo 8: Governança da rede (total_tenants_served + network_status).

Cobre:
  1. Contador no aceite do convite: pending→active recomputa total_tenants_served.
  2. Contador na ativação/revogação via POST/PATCH admin (link_walker_to_tenant /
     update_tenant_walker_access).
  3. Aprovação→perfil: ativar vínculo de walker sem profile cria WalkerNetworkProfile.
  4. Endpoint PATCH /{walker_user_id} (super_admin de network_status):
     - super_admin seta network_status e network_enabled → persiste + auditoria.
     - network_status inválido → 400.
     - não-super_admin → 403.
  5. Idempotência: chamar recompute 2x não duplica contador.
  6. Regressão: convite ainda transiciona pending→active (aceite não quebrou).

Padrão: SQLite StaticPool, FastAPI mínimo, dependency_overrides.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base, get_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.audit_log import AuditLog
from app.models.tenant import Tenant
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walker_network_profile import WalkerNetworkProfile
from app.routes import walker_network

# ── IDs de fixture ─────────────────────────────────────────────────────────────

ADMIN_ID = "sa-step8"
ADMIN_TENANT_ID = "admin-tenant-step8"
WALKER_ID = "walker-step8"
WALKER_B_ID = "walker-step8-b"
TENANT_A = "tenant-step8-a"
TENANT_B = "tenant-step8-b"


# ── Fábricas ──────────────────────────────────────────────────────────────────


def _make_db():
    """Cria um banco SQLite em memória com todas as tabelas."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    return db


def _seed(db, *, plan: str = "business", admin_role: str = "super_admin") -> None:
    """Seed mínimo: dois tenants business, um admin e dois walkers."""
    addon = plan in {"business", "enterprise"}
    db.add(Tenant(id=TENANT_A, name="Petshop A", slug="petshop-a", status="active", plan=plan, network_access_addon=addon))
    db.add(Tenant(id=TENANT_B, name="Petshop B", slug="petshop-b", status="active", plan=plan, network_access_addon=addon))
    # Tenant do admin (necessário para que o admin tenha tenant_id preenchido)
    db.add(Tenant(id=ADMIN_TENANT_ID, name="Admin HQ", slug="admin-hq", status="active", plan="business", network_access_addon=True))
    db.add(User(id=ADMIN_ID, email="sa@test.com", password_hash="x", role=admin_role, tenant_id=ADMIN_TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker-a@test.com", password_hash="x", role="walker", tenant_id=TENANT_A))
    db.add(User(id=WALKER_B_ID, email="walker-b@test.com", password_hash="x", role="walker", tenant_id=TENANT_A))
    db.commit()


def _build_admin_app(db, *, admin_role: str = "super_admin") -> TestClient:
    """FastAPI mínimo com o router admin, autenticado como ADMIN_ID."""
    app = FastAPI()
    app.include_router(walker_network.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, ADMIN_ID)
    return TestClient(app)


def _build_walker_app(db, walker_id: str) -> TestClient:
    """FastAPI mínimo com o walker_router, autenticado como walker_id."""
    app = FastAPI()
    app.include_router(walker_network.walker_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_walker_self_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: db.get(User, walker_id)
    return TestClient(app)


def _make_invite(db, tenant_id: str, walker_id: str, status: str = "pending") -> TenantWalkerAccess:
    access = TenantWalkerAccess(
        tenant_id=tenant_id,
        walker_user_id=walker_id,
        status=status,
        invited_at=datetime.utcnow() if status == "pending" else None,
    )
    db.add(access)
    db.commit()
    db.refresh(access)
    return access


# ══════════════════════════════════════════════════════════════════════════════
# 1. Contador no aceite do convite
# ══════════════════════════════════════════════════════════════════════════════


def test_total_tenants_served_increments_on_accept():
    """Walker sem profile aceita convite → total_tenants_served == 1."""
    db = _make_db()
    _seed(db)
    inv = _make_invite(db, TENANT_A, WALKER_ID)
    client = _build_walker_app(db, WALKER_ID)

    r = client.post(f"/walker/network/invites/{inv.id}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile is not None
    assert profile.total_tenants_served == 1


def test_total_tenants_served_with_two_accepted_tenants():
    """Walker aceita convites de dois tenants → total_tenants_served == 2."""
    db = _make_db()
    _seed(db)
    inv_a = _make_invite(db, TENANT_A, WALKER_ID)
    inv_b = _make_invite(db, TENANT_B, WALKER_ID)
    client = _build_walker_app(db, WALKER_ID)

    client.post(f"/walker/network/invites/{inv_a.id}/accept")
    client.post(f"/walker/network/invites/{inv_b.id}/accept")

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile.total_tenants_served == 2


def test_decline_does_not_increment_counter():
    """Recusa de convite NÃO incrementa total_tenants_served."""
    db = _make_db()
    _seed(db)
    inv = _make_invite(db, TENANT_A, WALKER_ID)
    client = _build_walker_app(db, WALKER_ID)

    client.post(f"/walker/network/invites/{inv.id}/decline")

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    # profile pode não existir (sem convite aceito) ou existir com 0
    if profile:
        assert profile.total_tenants_served == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2. Contador na ativação/revogação admin
# ══════════════════════════════════════════════════════════════════════════════


def test_post_link_active_increments_counter():
    """POST link_walker_to_tenant status=active → total_tenants_served recomputado."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    r = client.post(
        f"/admin/walker-network/tenants/{TENANT_A}",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    assert r.status_code == 200, r.text

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile is not None
    assert profile.total_tenants_served == 1


def test_patch_revoke_decrements_counter():
    """PATCH status=revoked → total_tenants_served cai para 0."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    # Ativa primeiro
    client.post(
        f"/admin/walker-network/tenants/{TENANT_A}",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile.total_tenants_served == 1

    # Revoga
    r = client.patch(
        f"/admin/walker-network/tenants/{TENANT_A}/walkers/{WALKER_ID}",
        json={"status": "revoked"},
    )
    assert r.status_code == 200, r.text

    db.refresh(profile)
    assert profile.total_tenants_served == 0


def test_counter_with_two_tenants_active_then_one_revoked():
    """Walker ativo em 2 tenants → revoga 1 → total_tenants_served == 1."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    client.post(
        f"/admin/walker-network/tenants/{TENANT_A}",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    client.post(
        f"/admin/walker-network/tenants/{TENANT_B}",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile.total_tenants_served == 2

    # Revoga apenas o tenant B
    client.patch(
        f"/admin/walker-network/tenants/{TENANT_B}/walkers/{WALKER_ID}",
        json={"status": "revoked"},
    )
    db.refresh(profile)
    assert profile.total_tenants_served == 1


# ══════════════════════════════════════════════════════════════════════════════
# 3. Aprovação→perfil: ativar vínculo de walker sem profile cria WalkerNetworkProfile
# ══════════════════════════════════════════════════════════════════════════════


def test_link_walker_creates_network_profile_if_missing():
    """Walker sem WalkerNetworkProfile: POST link ativo cria o profile automaticamente."""
    db = _make_db()
    _seed(db)

    # Confirma que não há profile
    assert db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first() is None

    client = _build_admin_app(db)
    r = client.post(
        f"/admin/walker-network/tenants/{TENANT_A}",
        json={"walker_user_id": WALKER_ID, "access_type": "shared_network", "status": "active"},
    )
    assert r.status_code == 200, r.text

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile is not None
    assert profile.total_tenants_served == 1


def test_patch_update_creates_network_profile_if_missing():
    """PATCH de vínculo existente sem profile cria o WalkerNetworkProfile."""
    db = _make_db()
    _seed(db)

    # Cria o vínculo diretamente no banco (sem passar pelo POST que cria profile)
    access = TenantWalkerAccess(
        tenant_id=TENANT_A,
        walker_user_id=WALKER_ID,
        status="pending",
    )
    db.add(access)
    db.commit()

    # Nenhum profile ainda
    assert db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first() is None

    client = _build_admin_app(db)
    r = client.patch(
        f"/admin/walker-network/tenants/{TENANT_A}/walkers/{WALKER_ID}",
        json={"status": "active"},
    )
    assert r.status_code == 200, r.text

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile is not None


# ══════════════════════════════════════════════════════════════════════════════
# 4. Endpoint PATCH /{walker_user_id} — super_admin de network_status
# ══════════════════════════════════════════════════════════════════════════════


def test_super_admin_can_suspend_walker():
    """super_admin seta network_status=suspended e network_enabled=False → persiste."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    r = client.patch(
        f"/admin/walker-network/{WALKER_ID}",
        json={"network_status": "suspended", "network_enabled": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["network_status"] == "suspended"
    assert body["network_enabled"] is False

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile.network_status == "suspended"
    assert profile.network_enabled is False


def test_super_admin_network_status_generates_audit_log():
    """Alterar network_status gera entrada em audit_log."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    r = client.patch(
        f"/admin/walker-network/{WALKER_ID}",
        json={"network_status": "blocked"},
    )
    assert r.status_code == 200, r.text

    logs = db.query(AuditLog).filter(AuditLog.action == "walker_network.status_updated").all()
    assert len(logs) == 1
    assert logs[0].entity_type == "walker_network_profile"
    assert logs[0].actor_user_id == ADMIN_ID


def test_invalid_network_status_returns_400():
    """network_status inválido → 400."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    r = client.patch(
        f"/admin/walker-network/{WALKER_ID}",
        json={"network_status": "lixo"},
    )
    assert r.status_code == 400, r.text


def test_non_super_admin_cannot_update_network_status():
    """Admin de tenant (role=admin) → 403 ao tentar alterar network_status."""
    db = _make_db()
    _seed(db, admin_role="admin")
    client = _build_admin_app(db, admin_role="admin")

    r = client.patch(
        f"/admin/walker-network/{WALKER_ID}",
        json={"network_status": "suspended"},
    )
    assert r.status_code == 403, r.text


def test_network_status_endpoint_only_network_enabled():
    """Enviar só network_enabled (sem network_status) também funciona."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    r = client.patch(
        f"/admin/walker-network/{WALKER_ID}",
        json={"network_enabled": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["network_enabled"] is False
    # network_status não alterado (mantém default "active")
    assert r.json()["network_status"] == "active"


def test_network_status_endpoint_walker_not_found():
    """PATCH com walker inexistente → 404."""
    db = _make_db()
    _seed(db)
    client = _build_admin_app(db)

    r = client.patch(
        "/admin/walker-network/nao-existe",
        json={"network_status": "suspended"},
    )
    assert r.status_code == 404, r.text


# ══════════════════════════════════════════════════════════════════════════════
# 5. Idempotência do recomputo
# ══════════════════════════════════════════════════════════════════════════════


def test_recompute_idempotent_double_call():
    """Chamar _recompute_tenants_served duas vezes não duplica o contador."""
    db = _make_db()
    _seed(db)

    # Cria vínculo ativo manualmente
    db.add(TenantWalkerAccess(tenant_id=TENANT_A, walker_user_id=WALKER_ID, status="active"))
    db.commit()

    from app.routes.walker_network import _recompute_tenants_served

    _recompute_tenants_served(WALKER_ID, db)
    _recompute_tenants_served(WALKER_ID, db)
    db.commit()

    profile = db.query(WalkerNetworkProfile).filter(WalkerNetworkProfile.walker_user_id == WALKER_ID).first()
    assert profile.total_tenants_served == 1  # ainda 1, não 2


# ══════════════════════════════════════════════════════════════════════════════
# 6. Regressão: aceite de convite ainda funciona corretamente
# ══════════════════════════════════════════════════════════════════════════════


def test_regression_accept_still_transitions_pending_to_active():
    """Regressão: aceitar convite ainda muda status de pending para active."""
    db = _make_db()
    _seed(db)
    inv = _make_invite(db, TENANT_A, WALKER_ID)
    client = _build_walker_app(db, WALKER_ID)

    r = client.post(f"/walker/network/invites/{inv.id}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"

    db.refresh(inv)
    assert inv.status == "active"
    assert inv.responded_at is not None


def test_regression_decline_still_transitions_pending_to_declined():
    """Regressão: recusar convite ainda muda status de pending para declined."""
    db = _make_db()
    _seed(db)
    inv = _make_invite(db, TENANT_A, WALKER_ID)
    client = _build_walker_app(db, WALKER_ID)

    r = client.post(f"/walker/network/invites/{inv.id}/decline")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "declined"

    db.refresh(inv)
    assert inv.status == "declined"
    assert inv.responded_at is not None


def test_regression_double_accept_returns_409():
    """Regressão: tentar aceitar convite já respondido → 409."""
    db = _make_db()
    _seed(db)
    inv = _make_invite(db, TENANT_A, WALKER_ID, status="active")
    client = _build_walker_app(db, WALKER_ID)

    r = client.post(f"/walker/network/invites/{inv.id}/accept")
    assert r.status_code == 409, r.text
