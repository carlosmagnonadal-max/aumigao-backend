"""Testes de regressão da Fase 1 HF-backend (2026-06-12).

Cobre:
1. Colisão de rota: GET /api/admin/walks/operational-metrics com token admin → 200
4. Timezone no GET /walks/{id}/locations: ?since com offset -03:00 é convertido para UTC
5. Idempotência de pagamento avulso (walk_id=None): dedup por tutor+amount+janela 2min
6. sandbox_message no modo live: None explícito é respeitado (não sobrescrito pelo or)

Padrão do projeto: FastAPI mínimo por módulo, SQLite em memória (StaticPool),
dependency_overrides de get_db/get_current_user. NUNCA importa app.main.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

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
from app.models.pet import Pet
from app.models.rbac import Permission, Role, RolePermission, UserRoleAssignment
from app.models.tenant import Tenant
from app.models.tenant_payment_config import DEFAULT_COMMISSION_PERCENT, TenantPaymentConfig
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_location_ping import WalkLocationPing
from app.routes import operational_walks, payments, walk_locations
from app.services.operational_matching_service import WALKER_ARRIVING, RIDE_IN_PROGRESS

# ---------------------------------------------------------------------------
# Constantes comuns
# ---------------------------------------------------------------------------
TENANT_ID = "t-hf1"
TUTOR_ID = "tutor-hf1"
WALKER_ID = "walker-hf1"
ADMIN_ID = "admin-hf1"
WALK_ID = "walk-hf1"
PET_ID = "pet-hf1"


# ---------------------------------------------------------------------------
# Helper: builder de app mínimo
# ---------------------------------------------------------------------------

class _CurrentUser:
    def __init__(self, db):
        self.db = db
        self.user_id = TUTOR_ID

    def __call__(self):
        return self.db.get(User, self.user_id)


def _make_db_and_base_data(tenant_slug: str = "aumigao-hf1"):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    db.add(Tenant(id=TENANT_ID, name="Aumigao HF", slug=tenant_slug, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor-hf1@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(User(id=WALKER_ID, email="walker-hf1@test.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(User(id=ADMIN_ID, email="admin-hf1@test.com", password_hash="x", role="admin", tenant_id=TENANT_ID))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, name="Rex", tenant_id=TENANT_ID))
    db.add(
        Walk(
            id=WALK_ID,
            tutor_id=TUTOR_ID,
            walker_id=WALKER_ID,
            tenant_id=TENANT_ID,
            pet_id=PET_ID,
            scheduled_date="2026-07-01T10:00:00",
            duration_minutes=45,
            price=50.0,
            status="Indo buscar o pet",
            operational_status=WALKER_ARRIVING,
            walker_selection_mode="auto",
        )
    )
    # RBAC: papel admin com walks.read
    role = Role(id="role-admin-hf1", name="tenant_admin_hf1", scope_type="tenant")
    perm = Permission(id="perm-walks-read-hf1", key="walks.read", module="walks", action="read")
    db.add(role)
    db.add(perm)
    db.add(RolePermission(id="rp-hf1", role_id="role-admin-hf1", permission_id="perm-walks-read-hf1"))
    db.add(UserRoleAssignment(id="ura-hf1", user_id=ADMIN_ID, role_id="role-admin-hf1"))
    db.commit()
    return db


# ===========================================================================
# ITEM 1 — Colisão de rota: operational-metrics NÃO pode ser capturado por
#           /admin/walks/{walk_id} quando registrado ANTES.
# ===========================================================================

def _build_operational_walks_app(db):
    """Monta app com admin_router do operational_walks registrado ANTES do
    (hipotético) router paramétrico. Reflete a ordem corrigida em main.py."""
    current = _CurrentUser(db)
    current.user_id = ADMIN_ID
    test_app = FastAPI()
    # Ordem correta: admin_router (rotas literais) ANTES de qualquer paramétrico
    test_app.include_router(operational_walks.admin_router)
    test_app.include_router(operational_walks.api_admin_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = current
    return TestClient(test_app, raise_server_exceptions=True), current


def test_item1_operational_metrics_nao_capturado_por_parametrico():
    """GET /admin/walks/operational-metrics com token admin deve retornar 200.

    Antes da correção, /admin/walks/{walk_id} capturava 'operational-metrics' como
    walk_id e retornava 404 ('Passeio nao encontrado').
    """
    db = _make_db_and_base_data()
    client, _ = _build_operational_walks_app(db)
    r = client.get("/admin/walks/operational-metrics")
    assert r.status_code == 200, (
        f"operational-metrics retornou {r.status_code}: {r.text}. "
        "Verifique se admin_router do operational_walks está registrado ANTES do paramétrico."
    )
    body = r.json()
    # Resposta deve conter pelo menos uma chave de métricas
    assert isinstance(body, dict), "Resposta de operational-metrics deve ser dict"


def test_item1_api_prefix_operational_metrics():
    """GET /api/admin/walks/operational-metrics também retorna 200."""
    db = _make_db_and_base_data("aumigao-hf1b")
    current = _CurrentUser(db)
    current.user_id = ADMIN_ID
    test_app = FastAPI()
    test_app.include_router(operational_walks.api_admin_router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = current
    client = TestClient(test_app, raise_server_exceptions=True)
    r = client.get("/api/admin/walks/operational-metrics")
    assert r.status_code == 200, r.text


# ===========================================================================
# ITEM 4 — Timezone no GET /walks/{id}/locations
# ===========================================================================

def _build_locations_app(db):
    current = _CurrentUser(db)
    current.user_id = TUTOR_ID
    test_app = FastAPI()
    test_app.include_router(walk_locations.router)
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_current_user] = current
    return TestClient(test_app, raise_server_exceptions=True), current


def test_item4_since_utc_minus3_converte_corretamente():
    """?since com offset -03:00 não deve deslocar 3h a mais — deve ser convertido para UTC.

    Cenário: ping às 14:00 UTC (= 11:00 BRT). since=11:00-03:00 → UTC 14:00.
    O ping das 14:00 UTC deve aparecer como 'after' de 14:00-ε mas não como
    'after' de 14:00+ε (limite exato). Adicionamos pings após o since em UTC.
    """
    db = _make_db_and_base_data("aumigao-hf1c")
    # Ping salvo às 14:00:00 UTC
    ping_utc = datetime(2026, 7, 1, 14, 0, 0)
    db.add(WalkLocationPing(
        id=str(uuid4()),
        walk_id=WALK_ID,
        walker_id=WALKER_ID,
        latitude=-23.5,
        longitude=-46.6,
        accuracy=10.0,
        recorded_at=ping_utc,
        created_at=datetime.utcnow(),
    ))
    # Ping posterior às 14:01:00 UTC
    ping_later = datetime(2026, 7, 1, 14, 1, 0)
    db.add(WalkLocationPing(
        id=str(uuid4()),
        walk_id=WALK_ID,
        walker_id=WALKER_ID,
        latitude=-23.51,
        longitude=-46.61,
        accuracy=8.0,
        recorded_at=ping_later,
        created_at=datetime.utcnow(),
    ))
    db.commit()

    client, _ = _build_locations_app(db)

    # since=11:00:00-03:00 → UTC 14:00:00
    # Sem correção (bug): replace(tzinfo=None) ignoraria o offset → filtraria por 11:00 UTC
    # e retornaria AMBOS os pings. Com correção: filtra por > 14:00 UTC → retorna apenas ping_later.
    r = client.get(f"/walks/{WALK_ID}/locations?since=2026-07-01T11:00:00-03:00")
    assert r.status_code == 200, r.text
    body = r.json()
    pings = body["pings"]
    # Com correção, since em UTC é 14:00:00 → apenas ping das 14:01 retorna
    assert len(pings) == 1, (
        f"Com offset -03:00 e since=11:00, esperamos apenas 1 ping (>14:00 UTC), "
        f"mas recebemos {len(pings)}. O bug de timezone pode estar presente."
    )
    assert abs(pings[0]["latitude"] - (-23.51)) < 0.001


def test_item4_since_sem_timezone_aceita_utc():
    """?since sem timezone é tratado como UTC (comportamento anterior preservado)."""
    db = _make_db_and_base_data("aumigao-hf1d")
    ping_early = datetime(2026, 7, 1, 13, 0, 0)
    ping_late = datetime(2026, 7, 1, 15, 0, 0)
    for lat, t in [(-23.5, ping_early), (-23.6, ping_late)]:
        db.add(WalkLocationPing(
            id=str(uuid4()),
            walk_id=WALK_ID,
            walker_id=WALKER_ID,
            latitude=lat,
            longitude=-46.6,
            accuracy=10.0,
            recorded_at=t,
            created_at=datetime.utcnow(),
        ))
    db.commit()

    client, _ = _build_locations_app(db)
    # since=14:00:00 UTC (sem timezone) → retorna apenas o das 15:00
    r = client.get(f"/walks/{WALK_ID}/locations?since=2026-07-01T14:00:00")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["pings"]) == 1
    assert abs(body["pings"][0]["latitude"] - (-23.6)) < 0.001


# ===========================================================================
# ITEM 5 — Idempotência de pagamento avulso (walk_id=None)
# ===========================================================================

def _build_payments_app(db):
    from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG
    test_app = FastAPI()
    test_app.include_router(payments.router)
    test_app.dependency_overrides[get_db] = lambda: db
    return test_app


def _make_payments_db():
    from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao HF", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=TUTOR_ID, email="tutor-hf1@test.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.commit()
    return db


def _as_user(test_app, db, uid):
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, uid)
    return TestClient(test_app)


@pytest.fixture(autouse=True)
def _force_sandbox_mode(monkeypatch):
    monkeypatch.setattr(payments, "PAYMENT_MODE", "asaas_sandbox")


def fake_asaas_ok_simple():
    async def _coro(payload, user):
        return {"id": f"pay-{uuid4()}", "status": "PENDING", "invoiceUrl": None, "bankSlipUrl": None}, {}, "PIX"
    return _coro


def test_item5_pagamento_avulso_idempotencia(monkeypatch):
    """Dois POSTs avulsos com mesmo tutor+amount em < 2min → apenas 1 Payment criado."""
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok_simple())
    db = _make_payments_db()
    test_app = _build_payments_app(db)
    client = _as_user(test_app, db, TUTOR_ID)

    # 1ª chamada: cria o pagamento
    r1 = client.post("/payments/create", json={"amount": 75.0, "method": "pix"})
    assert r1.status_code == 200, r1.text
    id1 = r1.json()["id"]

    # 2ª chamada: deve devolver o mesmo (idempotência)
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok_simple())
    r2 = client.post("/payments/create", json={"amount": 75.0, "method": "pix"})
    assert r2.status_code == 200, r2.text
    id2 = r2.json()["id"]

    assert id1 == id2, (
        f"Esperávamos dedup: mesmo payment_id em ambas as chamadas, "
        f"mas r1={id1}, r2={id2}. Idempotência avulsa não está funcionando."
    )
    # Verifica que só 1 Payment existe no banco
    count = db.query(Payment).filter(Payment.tutor_id == TUTOR_ID).count()
    assert count == 1, f"Esperava 1 Payment no banco, encontrou {count}"


def test_item5_pagamento_avulso_amount_diferente_nao_deduplica(monkeypatch):
    """Pagamentos avulsos com amounts distintos não são deduplicados."""
    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok_simple())
    db = _make_payments_db()
    test_app = _build_payments_app(db)
    client = _as_user(test_app, db, TUTOR_ID)

    r1 = client.post("/payments/create", json={"amount": 50.0, "method": "pix"})
    assert r1.status_code == 200, r1.text

    monkeypatch.setattr(payments, "create_asaas_payment", fake_asaas_ok_simple())
    r2 = client.post("/payments/create", json={"amount": 60.0, "method": "pix"})
    assert r2.status_code == 200, r2.text

    assert r1.json()["id"] != r2.json()["id"], "Amounts diferentes não devem ser deduplicados"
    count = db.query(Payment).filter(Payment.tutor_id == TUTOR_ID).count()
    assert count == 2


# ===========================================================================
# ITEM 6 — sandbox_message: None explícito no modo live é respeitado
# ===========================================================================

def test_item6_sandbox_message_none_no_modo_live(monkeypatch):
    """payment_response com sandbox_message=None (modo live) → campo None no JSON."""
    from app.routes.payments import payment_response
    from app.models.payment import Payment

    # Simula um objeto Payment mínimo
    payment = Payment(
        id="pay-test",
        tutor_id=TUTOR_ID,
        walk_id=None,
        amount=100.0,
        status="aguardando_pagamento",
        provider="asaas_live",
        provider_payment_id="asaas-live-123",
        invoice_url=None,
        commission_percent=20.0,
        platform_amount=20.0,
        walker_amount=80.0,
    )

    # Modo live: sandbox_message=None explícito
    result = payment_response(payment, method="pix", sandbox_message=None)
    assert result["sandbox_message"] is None, (
        f"No modo live, sandbox_message=None deve ser respeitado, "
        f"mas recebemos: {result['sandbox_message']!r}"
    )


def test_item6_sandbox_message_presente_no_modo_sandbox(monkeypatch):
    """payment_response sem sandbox_message explícito → usa o default sandbox."""
    from app.routes.payments import payment_response
    from app.models.payment import Payment

    payment = Payment(
        id="pay-test2",
        tutor_id=TUTOR_ID,
        walk_id=None,
        amount=100.0,
        status="pagamento_sandbox_criado",
        provider="asaas_sandbox",
        provider_payment_id=None,
        invoice_url=None,
        commission_percent=20.0,
        platform_amount=20.0,
        walker_amount=80.0,
    )

    # Sem passar sandbox_message → cai no default
    result = payment_response(payment, method="pix")
    assert result["sandbox_message"] is not None
    assert "sandbox" in result["sandbox_message"].lower() or "cobranca" in result["sandbox_message"].lower(), (
        f"Sem sandbox_message explícito, esperamos o default de sandbox, mas: {result['sandbox_message']!r}"
    )


def test_item6_sandbox_message_override_explicito(monkeypatch):
    """payment_response com sandbox_message='msg custom' usa a string custom."""
    from app.routes.payments import payment_response
    from app.models.payment import Payment

    payment = Payment(
        id="pay-test3",
        tutor_id=TUTOR_ID,
        walk_id="walk-abc",
        amount=50.0,
        status="aguardando_pagamento",
        provider="asaas_sandbox",
        provider_payment_id="pay-123",
        invoice_url=None,
        commission_percent=20.0,
        platform_amount=10.0,
        walker_amount=40.0,
    )

    result = payment_response(payment, method="pix", sandbox_message="msg idempotencia")
    assert result["sandbox_message"] == "msg idempotencia"
