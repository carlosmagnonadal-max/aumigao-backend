"""Testes — Fase 1 Passo 4: Financeiro por tenant + comissão por par.

Cobre (spec §E):
  1. by_tenant: walker com Payments pagos em 2 tenants → 2 entradas, saldos corretos.
  2. Buckets: status pago/pendente/processando → available/pending/processing.
  3. Saque por tenant: POST /walker/withdrawals com tenant_id → cria Payment(-amount,
     tenant_id); by_tenant[T1].available cai; saque > saldo do tenant → 400.
  4. Saque legado: POST sem tenant_id → valida saldo global, Payment tenant_id NULL.
  5. Superset: resposta de earnings mantém todas as chaves antigas.
  6. Comissão por par:
       a) get_commission_percent(db, T1, walker_id=W) com TWA.commission_percent=15 → 15.0
       b) Sem TWA.commission_percent → cai no config/plano (fallback).
       c) build_payment_split(..., walker_id=W) usa 15%.
       d) get_commission_percent(db, T1) sem walker_id → comportamento original.

Padrão: FastAPI mínimo com apenas o router de walker, SQLite em memória (StaticPool),
dependency_overrides cobrindo get_db, get_global_db, get_current_user E
get_walker_self_db (pois earnings/withdrawals usam get_walker_self_db e get_db
respectivamente).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from uuid import uuid4

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.constants import PAID_PAYMENT_STATUSES
from app.core.database import Base, get_db, get_global_db, get_walker_self_db
from app.dependencies.auth import get_current_user
from app.models.payment import Payment
from app.models.tenant import Tenant, TenantBranding
from app.models.tenant_payment_config import TenantPaymentConfig
from app.models.tenant_walker_access import TenantWalkerAccess
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.pet import Pet
from app.routes import walker as walker_module
from app.services.payment_split_service import (
    build_payment_split,
    get_commission_percent,
)

# ─── IDs de fixture ───────────────────────────────────────────────────────────
WALKER_ID = "walker-p4"
TUTOR_ID = "tutor-p4"
TENANT_1 = "tenant-p4-one"
TENANT_2 = "tenant-p4-two"
PET_ID = "pet-p4"

# Status pago canônico para criar payments confirmados nos testes
PAID_STATUS = "paid"
assert PAID_STATUS in PAID_PAYMENT_STATUSES


# ─── Builder de app mínimo ────────────────────────────────────────────────────

def _make_engine_and_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def build_app(db):
    """Monta FastAPI mínimo com o router de walker e overrides de dependência."""
    test_app = FastAPI()
    test_app.include_router(walker_module.router)
    # Tanto earnings (get_walker_self_db) quanto withdrawals (get_db) precisam
    # apontar para o mesmo banco em memória.
    test_app.dependency_overrides[get_db] = lambda: db
    test_app.dependency_overrides[get_global_db] = lambda: db
    test_app.dependency_overrides[get_walker_self_db] = lambda: db
    return test_app


def as_walker(test_app, db):
    """Autentica como o walker de teste."""
    test_app.dependency_overrides[get_current_user] = lambda: db.get(User, WALKER_ID)
    return TestClient(test_app)


# ─── Helper de seed básico ────────────────────────────────────────────────────

def _seed_base(db):
    """Insere os registros mínimos necessários para todos os testes."""
    # Dois tenants
    db.add(Tenant(id=TENANT_1, name="Tenant Um", slug="tenant-um", status="active", plan="business"))
    db.add(Tenant(id=TENANT_2, name="Tenant Dois", slug="tenant-dois", status="active", plan="starter"))

    # Tutor
    db.add(User(id=TUTOR_ID, email="tutor@p4.com", password_hash="x", role="cliente", tenant_id=TENANT_1))

    # Walker
    db.add(User(id=WALKER_ID, email="walker@p4.com", password_hash="x", role="walker", tenant_id=TENANT_1))
    db.add(WalkerProfile(
        id=str(uuid4()),
        user_id=WALKER_ID,
        status="active",
        active_as_walker=True,
        pix_key="walker@pix.com",  # FIX 6e: necessário para POST /walker/withdrawals
    ))

    # Pet
    db.add(Pet(id=PET_ID, name="Rex", species="cachorro", tutor_id=TUTOR_ID, tenant_id=TENANT_1))

    db.commit()


def _make_walk(db, tenant_id: str, price: float = 50.0) -> Walk:
    """Cria um walk concluído associado ao walker de teste.

    status="Finalizado" é o valor canônico usado por _completed_walks
    (Walk.status == "Finalizado") — zero-regressão.
    scheduled_date e duration_minutes são NOT NULL no schema SQLite.
    """
    w = Walk(
        id=str(uuid4()),
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        pet_id=PET_ID,
        tenant_id=tenant_id,
        status="Finalizado",  # canônico para _completed_walks
        price=price,
        scheduled_date="2026-01-01",
        duration_minutes=30,
    )
    db.add(w)
    db.flush()
    return w


def _make_payment(
    db,
    walk: Walk,
    walker_amount: float,
    status: str = PAID_STATUS,
    tenant_id: str | None = None,
) -> Payment:
    """Cria um Payment para o walk com walker_amount e tenant_id especificados."""
    p = Payment(
        id=str(uuid4()),
        tutor_id=TUTOR_ID,
        walk_id=walk.id,
        amount=walk.price,
        status=status,
        provider="asaas",
        walker_amount=walker_amount,
        commission_percent=10.0,
        platform_amount=walk.price - walker_amount,
        tenant_id=tenant_id,
    )
    db.add(p)
    db.flush()
    return p


# ═══════════════════════════════════════════════════════════════════════════════
# 1. by_tenant — 2 tenants, saldos corretos
# ═══════════════════════════════════════════════════════════════════════════════

def test_by_tenant_two_tenants():
    """Walker com Payments pagos em T1 (100) e T2 (50) → 2 entradas no by_tenant."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    w1 = _make_walk(db, TENANT_1, price=120.0)
    _make_payment(db, w1, walker_amount=100.0, status=PAID_STATUS, tenant_id=TENANT_1)

    w2 = _make_walk(db, TENANT_2, price=60.0)
    _make_payment(db, w2, walker_amount=50.0, status=PAID_STATUS, tenant_id=TENANT_2)
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.get("/walker/earnings")
    assert r.status_code == 200, r.text
    body = r.json()

    assert "by_tenant" in body
    bt = {entry["tenant_id"]: entry for entry in body["by_tenant"]}
    assert TENANT_1 in bt
    assert TENANT_2 in bt
    assert bt[TENANT_1]["available"] == 100.0
    assert bt[TENANT_2]["available"] == 50.0

    # consolidated deve somar os dois
    assert body["consolidated"]["available"] == 150.0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Buckets: paid / pending / processing
# ═══════════════════════════════════════════════════════════════════════════════

def test_by_tenant_status_buckets():
    """Payments com status diferente caem nos buckets corretos."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    # Pago → available
    w_paid = _make_walk(db, TENANT_1, price=100.0)
    _make_payment(db, w_paid, walker_amount=80.0, status="paid", tenant_id=TENANT_1)

    # Pendente → pending
    w_pend = _make_walk(db, TENANT_1, price=50.0)
    _make_payment(db, w_pend, walker_amount=40.0, status="pending", tenant_id=TENANT_1)

    # Processando → processing
    w_proc = _make_walk(db, TENANT_1, price=30.0)
    _make_payment(db, w_proc, walker_amount=25.0, status="em_processamento", tenant_id=TENANT_1)

    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.get("/walker/earnings")
    assert r.status_code == 200, r.text
    body = r.json()

    bt = {entry["tenant_id"]: entry for entry in body["by_tenant"]}
    assert TENANT_1 in bt
    entry = bt[TENANT_1]
    assert entry["available"] == 80.0
    assert entry["pending"] == 40.0
    assert entry["processing"] == 25.0
    assert entry["total"] == 145.0


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Saque por tenant
# ═══════════════════════════════════════════════════════════════════════════════

def test_withdrawal_by_tenant_creates_payment_with_tenant_id():
    """POST /walker/withdrawals com tenant_id → cria Payment com tenant_id."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    w1 = _make_walk(db, TENANT_1, price=120.0)
    _make_payment(db, w1, walker_amount=100.0, status=PAID_STATUS, tenant_id=TENANT_1)
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.post("/walker/withdrawals", json={"amount": 30.0, "tenant_id": TENANT_1})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["amount"] == 30.0

    # Verifica Payment criado no banco
    pmt = db.query(Payment).filter(
        Payment.provider == "pix",
        Payment.walk_id.is_(None),
        Payment.amount == -30.0,
    ).first()
    assert pmt is not None
    assert pmt.tenant_id == TENANT_1


def test_withdrawal_by_tenant_updates_available_balance():
    """Após saque de 30 em T1 (saldo 100), available[T1] deve cair para 70."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    w1 = _make_walk(db, TENANT_1, price=120.0)
    _make_payment(db, w1, walker_amount=100.0, status=PAID_STATUS, tenant_id=TENANT_1)
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    # Faz o saque
    r = client.post("/walker/withdrawals", json={"amount": 30.0, "tenant_id": TENANT_1})
    assert r.status_code == 200, r.text

    # Verifica o saldo após o saque
    r2 = client.get("/walker/earnings")
    assert r2.status_code == 200, r2.text
    body = r2.json()
    bt = {entry["tenant_id"]: entry for entry in body["by_tenant"]}
    assert bt[TENANT_1]["available"] == 70.0


def test_withdrawal_by_tenant_exceeds_balance_returns_400():
    """Saque maior que o saldo do tenant → 400."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    w1 = _make_walk(db, TENANT_1, price=60.0)
    _make_payment(db, w1, walker_amount=50.0, status=PAID_STATUS, tenant_id=TENANT_1)
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.post("/walker/withdrawals", json={"amount": 80.0, "tenant_id": TENANT_1})
    assert r.status_code == 400
    assert "insuficiente" in r.json()["detail"].lower()


def test_withdrawal_by_tenant_min_amount_enforced():
    """Saque < 20 → 400 mesmo com tenant_id."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    w1 = _make_walk(db, TENANT_1, price=100.0)
    _make_payment(db, w1, walker_amount=90.0, status=PAID_STATUS, tenant_id=TENANT_1)
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.post("/walker/withdrawals", json={"amount": 5.0, "tenant_id": TENANT_1})
    assert r.status_code == 400
    assert "minimo" in r.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Saque legado — sem tenant_id (comportamento original intacto)
# ═══════════════════════════════════════════════════════════════════════════════

def test_withdrawal_legacy_no_tenant_id():
    """POST sem tenant_id → Payment criado com tenant_id=NULL (comportamento legado)."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    # Não precisa de Payment com split calculado: _available_balance usa fallback walk.price
    w1 = _make_walk(db, TENANT_1, price=100.0)
    # Walk concluído, sem Payment com walker_amount → fallback usa walk.price
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.post("/walker/withdrawals", json={"amount": 20.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    # Payment deve ter tenant_id NULL
    pmt = db.query(Payment).filter(
        Payment.provider == "pix",
        Payment.walk_id.is_(None),
    ).first()
    assert pmt is not None
    assert pmt.tenant_id is None


def test_withdrawal_legacy_insufficient_global_balance():
    """Sem tenant_id, saldo global insuficiente → 400 com texto legado."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)
    db.commit()  # walker sem walks concluídos → saldo 0

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.post("/walker/withdrawals", json={"amount": 50.0})
    assert r.status_code == 400
    # Mensagem legada intacta
    assert "insuficiente" in r.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Superset — todas as chaves antigas presentes
# ═══════════════════════════════════════════════════════════════════════════════

def test_earnings_superset_has_all_legacy_keys():
    """GET /walker/earnings deve conter TODAS as chaves do contrato original."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.get("/walker/earnings")
    assert r.status_code == 200, r.text
    body = r.json()

    legacy_keys = {
        "available_balance",
        "weekly_total",
        "completed_walks",
        "tips",
        "walk_earnings",
        "total_with_tips",
        "tips_pending_review",
        "tips_policy",
        "goal_total_walks",
        "future_reward_preview",
        "level",
        "score",
        "transactions",
    }
    missing = legacy_keys - set(body.keys())
    assert not missing, f"Chaves legadas ausentes: {missing}"

    # Novas chaves também presentes
    assert "by_tenant" in body
    assert "consolidated" in body


def test_earnings_transactions_have_tenant_fields():
    """Cada item de transactions deve ter tenant_id e tenant_name."""
    _, Session = _make_engine_and_session()
    db = Session()
    _seed_base(db)

    w1 = _make_walk(db, TENANT_1, price=50.0)
    # Walk concluído: status completed → aparece em _completed_walks
    db.commit()

    test_app = build_app(db)
    client = as_walker(test_app, db)

    r = client.get("/walker/earnings")
    assert r.status_code == 200, r.text
    body = r.json()

    # Deve ter pelo menos 1 transação (walk concluído)
    transactions = body["transactions"]
    assert len(transactions) >= 1

    for t in transactions:
        assert "tenant_id" in t, f"Campo tenant_id ausente em {t['id']}"
        assert "tenant_name" in t, f"Campo tenant_name ausente em {t['id']}"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Comissão por par — get_commission_percent / build_payment_split
# ═══════════════════════════════════════════════════════════════════════════════

def test_commission_by_pair_twa_commission_takes_precedence():
    """TWA.commission_percent=15 → get_commission_percent retorna 15.0."""
    _, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_1, name="T1", slug="t1-p4", status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="w@x.com", password_hash="x", role="walker", tenant_id=TENANT_1))
    # TenantPaymentConfig com comissão 20%
    db.add(TenantPaymentConfig(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        commission_percent=20.0,
        active=True,
    ))
    # TenantWalkerAccess com comissão negociada 15%
    from decimal import Decimal
    db.add(TenantWalkerAccess(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        walker_user_id=WALKER_ID,
        status="active",
        commission_percent=Decimal("15.00"),
    ))
    db.commit()

    result = get_commission_percent(db, TENANT_1, walker_id=WALKER_ID)
    assert result == 15.0


def test_commission_by_pair_no_twa_commission_falls_back_to_config():
    """Sem TWA.commission_percent → usa TenantPaymentConfig (20%)."""
    _, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_1, name="T1", slug="t1-p4b", status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="w@x2.com", password_hash="x", role="walker", tenant_id=TENANT_1))
    db.add(TenantPaymentConfig(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        commission_percent=20.0,
        active=True,
    ))
    # TWA sem commission_percent (NULL)
    db.add(TenantWalkerAccess(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        walker_user_id=WALKER_ID,
        status="active",
        commission_percent=None,
    ))
    db.commit()

    result = get_commission_percent(db, TENANT_1, walker_id=WALKER_ID)
    assert result == 20.0


def test_commission_by_pair_no_twa_at_all_falls_back_to_plan():
    """Sem TWA e sem TenantPaymentConfig → fallback de plano (business=8%)."""
    _, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_1, name="T1", slug="t1-p4c", status="active", plan="business"))
    db.commit()

    result = get_commission_percent(db, TENANT_1, walker_id=WALKER_ID)
    # Nenhuma config, plan=business → 8%
    assert result == 8.0


def test_commission_without_walker_id_unchanged():
    """get_commission_percent sem walker_id = comportamento original (config→plano)."""
    _, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_1, name="T1", slug="t1-p4d", status="active", plan="starter"))
    db.add(TenantPaymentConfig(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        commission_percent=18.0,
        active=True,
    ))
    # TWA com commission_percent=5 — não deve ser usada quando walker_id=None
    from decimal import Decimal
    db.add(TenantWalkerAccess(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        walker_user_id=WALKER_ID,
        status="active",
        commission_percent=Decimal("5.00"),
    ))
    db.commit()

    # Sem walker_id: nível 1 não é consultado → usa TenantPaymentConfig (18%)
    result = get_commission_percent(db, TENANT_1)
    assert result == 18.0


def test_build_payment_split_uses_twa_commission():
    """build_payment_split com walker_id usa comissão 15% (TWA) em vez de 20% (config)."""
    _, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_1, name="T1", slug="t1-p4e", status="active", plan="business"))
    db.add(TenantPaymentConfig(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        commission_percent=20.0,
        active=True,
    ))
    from decimal import Decimal
    db.add(TenantWalkerAccess(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        walker_user_id=WALKER_ID,
        status="active",
        commission_percent=Decimal("15.00"),
    ))
    db.commit()

    split = build_payment_split(db, TENANT_1, 100.0, walker_id=WALKER_ID)
    assert split["commission_percent"] == 15.0
    assert split["walker_amount"] == 85.0
    assert split["platform_amount"] == 15.0


def test_build_payment_split_without_walker_id_unchanged():
    """build_payment_split sem walker_id usa config (20%) — zero-regressão."""
    _, Session = _make_engine_and_session()
    db = Session()

    db.add(Tenant(id=TENANT_1, name="T1", slug="t1-p4f", status="active", plan="business"))
    db.add(TenantPaymentConfig(
        id=str(uuid4()),
        tenant_id=TENANT_1,
        commission_percent=20.0,
        active=True,
    ))
    db.commit()

    split = build_payment_split(db, TENANT_1, 100.0)
    assert split["commission_percent"] == 20.0
    assert split["walker_amount"] == 80.0
