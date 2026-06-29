"""Testes para os 3 itens fiscais/contábeis (Items 2, 3, 4).

ITEM 2: provisão fiscal no passeio de assinatura (subscription_walk).
ITEM 3: breakage de créditos expirados/cancelados.
ITEM 4: ledger contábil do ciclo de crédito (liability, revenue, breakage).

Princípio: NÃO muda fluxo de dinheiro. Concessão/consumo/estorno de crédito
devem ser idênticos ao estado anterior. O ledger é best-effort.
"""
import os
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # registra todas as tabelas no Base (incluindo CreditLedgerEntry)
from app.core.database import Base
from app.models.fiscal import PaymentProvision, TenantFiscalConfig
from app.models.pet import Pet
from app.models.credit_ledger import (
    CreditLedgerEntry,
    LEDGER_LIABILITY_CREATED,
    LEDGER_REVENUE_RECOGNIZED,
    LEDGER_BREAKAGE_RECOGNIZED,
)
from app.models.payment import Payment
from app.models.recurring_plan import (
    RecurringPlan, TutorSubscription, SUBSCRIPTION_ACTIVE, SUBSCRIPTION_CANCELLED,
)
from app.models.tenant import Tenant, TenantFeature
from app.models.user import User
from app.models.walk import Walk
from app.services.recurring_plan_service import (
    subscribe,
    cancel_subscription,
    get_active_subscription,
    consume_credit_if_available,
    refund_credit_for_walk,
    grant_credits_on_payment,
)
from app.services.credit_expiry_service import sweep_expired_credits, recognize_breakage_on_cancel
from app.services.credit_ledger_service import record_liability_safe, record_revenue_recognized_safe
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-fiscal"
TUTOR_ID = "tutor-fiscal"


def _make_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


PET_ID = "pet-fiscal"


def _make_db():
    engine = _make_engine()
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(TenantFeature(tenant_id=TENANT_ID, feature_key="recurring_plans", enabled=True))
    db.add(User(id=TUTOR_ID, email="tutor@fiscal.com", password_hash="x", role="cliente", tenant_id=TENANT_ID))
    db.add(Pet(id=PET_ID, tutor_id=TUTOR_ID, tenant_id=TENANT_ID, name="Bolinha"))
    db.commit()
    return db


def _tenant(db):
    return db.get(Tenant, TENANT_ID)


def _make_plan(db, walks_per_cycle=4, price=80.0):
    tenant = _tenant(db)
    plan = RecurringPlan(
        tenant_id=tenant.id, name="Plano Mensal", price=price,
        walks_per_cycle=walks_per_cycle, interval="monthly", active=True,
    )
    db.add(plan); db.commit(); db.refresh(plan)
    return plan


def _make_subscription(db, walks_per_cycle=4, price=80.0, status=SUBSCRIPTION_ACTIVE, credits_remaining=None):
    plan = _make_plan(db, walks_per_cycle=walks_per_cycle, price=price)
    tenant = _tenant(db)
    sub = TutorSubscription(
        tenant_id=TENANT_ID,
        plan_id=plan.id,
        tutor_id=TUTOR_ID,
        status=status,
        price=price,
        walks_per_cycle=walks_per_cycle,
        credits_remaining=credits_remaining if credits_remaining is not None else walks_per_cycle,
        credits_granted=True,
        current_period_start=datetime.utcnow(),
        current_period_end=datetime.utcnow() + timedelta(days=30),
    )
    db.add(sub); db.commit(); db.refresh(sub)
    return sub


# ─── ITEM 2: provisão fiscal no passeio de assinatura ─────────────────────────

class TestItem2SubscriptionWalkProvision:
    """Provisão fiscal para passeio coberto por assinatura (subscription_walk)."""

    def _db_with_fiscal_config(self):
        db = _make_db()
        from app.services.fiscal_config_service import upsert_fiscal_config
        upsert_fiscal_config(db, TENANT_ID, {"commission_tax_percent": 10, "walker_tax_percent": 2})
        return db

    def _make_payment(self, db, walk_id, provider="subscription_walk", tenant_id=TENANT_ID):
        from uuid import uuid4
        p = Payment(
            id=str(uuid4()),
            tenant_id=tenant_id,
            tutor_id=TUTOR_ID,
            walk_id=walk_id,
            amount=50.0,
            status="paid",
            provider=provider,
            commission_percent=10.0,
            platform_amount=5.0,
            walker_amount=45.0,
        )
        db.add(p); db.commit(); db.refresh(p)
        return p

    def test_subscription_walk_generates_provision(self):
        """_ensure_internal_walk_payment cria provisão para passeio de assinatura."""
        import app.routes.admin as admin_mod
        db = self._db_with_fiscal_config()
        sub = _make_subscription(db)

        from uuid import uuid4
        db.add(User(id="walker-f", email="wf@x.com", password_hash="x", role="passeador", tenant_id=TENANT_ID))
        walk = Walk(
            id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=TENANT_ID,
            pet_id=PET_ID,
            scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
            status="Agendado", subscription_id=sub.id, credit_refunded=False,
            walker_id="walker-f",
        )
        db.add(walk); db.commit()

        admin_mod._ensure_internal_walk_payment(walk, db)
        db.commit()

        provisions = db.query(PaymentProvision).filter(PaymentProvision.tenant_id == TENANT_ID).all()
        assert len(provisions) == 1, f"Esperado 1 provisão, got {len(provisions)}"
        assert provisions[0].revenue_type == "walk_commission"

    def test_subscription_walk_provision_not_duplicated(self):
        """Chamar _ensure_internal_walk_payment 2× não duplica provisão."""
        import app.routes.admin as admin_mod
        db = self._db_with_fiscal_config()
        sub = _make_subscription(db)

        from uuid import uuid4
        db.add(User(id="walker-g", email="wg@x.com", password_hash="x", role="passeador", tenant_id=TENANT_ID))
        walk = Walk(
            id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=TENANT_ID,
            pet_id=PET_ID,
            scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
            status="Agendado", subscription_id=sub.id, credit_refunded=False,
            walker_id="walker-g",
        )
        db.add(walk); db.commit()

        admin_mod._ensure_internal_walk_payment(walk, db)
        db.commit()
        # 2ª chamada: deve retornar o payment existente sem duplicar provisão
        admin_mod._ensure_internal_walk_payment(walk, db)
        db.commit()

        count = db.query(PaymentProvision).filter(PaymentProvision.tenant_id == TENANT_ID).count()
        assert count == 1, f"Esperado 1 provisão (idempotência), got {count}"

    def test_avulso_walk_provision_unchanged(self):
        """Passeio avulso (provider=internal) NÃO é afetado pela mudança do Item 2."""
        import app.routes.admin as admin_mod
        db = self._db_with_fiscal_config()

        from uuid import uuid4
        db.add(User(id="walker-h", email="wh@x.com", password_hash="x", role="passeador", tenant_id=TENANT_ID))
        walk = Walk(
            id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=TENANT_ID,
            pet_id=PET_ID,
            scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
            status="Agendado", subscription_id=None, credit_refunded=False,
            walker_id="walker-h",
        )
        db.add(walk); db.commit()

        admin_mod._ensure_internal_walk_payment(walk, db)
        db.commit()

        # Provisão ainda é gerada (não regrediu)
        pay = db.query(Payment).filter(Payment.walk_id == walk.id).first()
        assert pay is not None
        assert pay.provider == "internal"

    def test_provision_safe_never_raises(self, monkeypatch):
        """Falha na provisão não propaga exceção."""
        import app.routes.admin as admin_mod
        from app.services import provision_service as ps

        original = ps.compute_and_store_provision

        def boom(*a, **k):
            raise RuntimeError("falha simulada")

        monkeypatch.setattr(ps, "compute_and_store_provision", boom)

        db = _make_db()
        sub = _make_subscription(db)

        from uuid import uuid4
        db.add(User(id="walker-i", email="wi@x.com", password_hash="x", role="passeador", tenant_id=TENANT_ID))
        walk = Walk(
            id=str(uuid4()), tutor_id=TUTOR_ID, tenant_id=TENANT_ID,
            pet_id=PET_ID,
            scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
            status="Agendado", subscription_id=sub.id, credit_refunded=False,
            walker_id="walker-i",
        )
        db.add(walk); db.commit()

        # Não deve levantar — best-effort
        admin_mod._ensure_internal_walk_payment(walk, db)


# ─── ITEM 3: breakage de créditos expirados/cancelados ───────────────────────

class TestItem3Breakage:

    def test_cancelled_subscription_with_credits_recognized_as_breakage(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, credits_remaining=3, status=SUBSCRIPTION_CANCELLED)

        result = sweep_expired_credits(db)

        assert result["recognized"] >= 1
        entries = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_BREAKAGE_RECOGNIZED,
        ).all()
        assert len(entries) == 1
        assert entries[0].credits_count == 3

    def test_cancelled_subscription_credits_zeroed_after_breakage(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)

        sweep_expired_credits(db)
        db.refresh(sub)

        assert sub.credits_remaining == 0

    def test_breakage_not_duplicated_on_rerun(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)

        r1 = sweep_expired_credits(db)
        r2 = sweep_expired_credits(db)

        assert r1["recognized"] == 1
        assert r2["recognized"] == 0  # já reconhecido
        count = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.event_type == LEDGER_BREAKAGE_RECOGNIZED,
            CreditLedgerEntry.subscription_id == sub.id,
        ).count()
        assert count == 1

    def test_active_subscription_with_credits_not_breakage(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, credits_remaining=4, status=SUBSCRIPTION_ACTIVE)
        # Período ainda válido (future)
        sub.current_period_end = datetime.utcnow() + timedelta(days=10)
        db.add(sub); db.commit()

        result = sweep_expired_credits(db)
        assert result["recognized"] == 0

    def test_active_expired_period_recognized_as_breakage(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, credits_remaining=2, status=SUBSCRIPTION_ACTIVE)
        sub.current_period_end = datetime.utcnow() - timedelta(days=1)
        db.add(sub); db.commit()

        result = sweep_expired_credits(db)
        assert result["recognized"] == 1

    def test_no_credits_no_breakage(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, credits_remaining=0, status=SUBSCRIPTION_CANCELLED)

        result = sweep_expired_credits(db)
        assert result["recognized"] == 0

    def test_cancel_subscription_triggers_breakage(self):
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4)
        sub = subscribe(db, _tenant(db), TUTOR_ID, plan.id)
        sub.credits_remaining = 3
        db.add(sub); db.commit()

        cancel_subscription(db, TENANT_ID, TUTOR_ID)

        entries = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.event_type == LEDGER_BREAKAGE_RECOGNIZED,
        ).all()
        assert len(entries) == 1
        assert entries[0].credits_count == 3

    def test_cancel_subscription_without_credits_no_breakage(self):
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4)
        sub = subscribe(db, _tenant(db), TUTOR_ID, plan.id)
        sub.credits_remaining = 0
        db.add(sub); db.commit()

        cancel_subscription(db, TENANT_ID, TUTOR_ID)

        entries = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.event_type == LEDGER_BREAKAGE_RECOGNIZED,
        ).all()
        assert len(entries) == 0

    def test_breakage_safe_never_raises(self, monkeypatch):
        """Falha no breakage não propaga exceção."""
        db = _make_db()
        sub = _make_subscription(db, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)

        import app.services.credit_expiry_service as ces
        monkeypatch.setattr(ces, "_has_breakage_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

        # Não deve levantar
        result = sweep_expired_credits(db)
        # Pode retornar 0 recognized por causa do erro silenciado
        assert isinstance(result, dict)

    def test_credit_amount_is_correct(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, price=80.0, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)

        sweep_expired_credits(db)

        entry = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_BREAKAGE_RECOGNIZED,
        ).first()
        assert entry is not None
        # unit_value = 80.0 / 4 = 20.0; 2 × 20 = 40.0
        assert abs(float(entry.unit_value) - 20.0) < 0.01
        assert abs(float(entry.total_value) - 40.0) < 0.01

    def test_sweep_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("CREDIT_LEDGER_ENABLED", "false")
        db = _make_db()
        _make_subscription(db, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)

        result = sweep_expired_credits(db)
        assert result.get("skipped") is True
        assert result["processed"] == 0


# ─── ITEM 4: ledger contábil (liability, revenue, breakage) ──────────────────

class TestItem4CreditLedger:

    def test_record_liability_on_grant(self):
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4, price=80.0)
        sub = subscribe(db, _tenant(db), TUTOR_ID, plan.id)
        # subscribe() (síncrono) já chama record_liability_safe internamente
        entries = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).all()
        assert len(entries) == 1
        assert entries[0].credits_count == 4
        assert abs(float(entries[0].unit_value) - 20.0) < 0.01

    def test_liability_idempotent(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, price=80.0)

        record_liability_safe(db, sub, payment_id=None)
        db.commit()
        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        count = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).count()
        assert count == 1

    def test_record_revenue_recognized_on_consume(self):
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4, price=80.0)
        sub = subscribe(db, _tenant(db), TUTOR_ID, plan.id)

        walk_id = "walk-revenue-test"
        record_revenue_recognized_safe(db, sub, walk_id)
        db.commit()

        entries = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_REVENUE_RECOGNIZED,
        ).all()
        assert len(entries) == 1
        assert entries[0].walk_id == walk_id
        assert abs(float(entries[0].unit_value) - 20.0) < 0.01

    def test_revenue_recognized_idempotent_per_walk(self):
        db = _make_db()
        sub = _make_subscription(db)
        walk_id = "walk-dedup"

        record_revenue_recognized_safe(db, sub, walk_id)
        db.commit()
        record_revenue_recognized_safe(db, sub, walk_id)
        db.commit()

        count = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.walk_id == walk_id,
            CreditLedgerEntry.event_type == LEDGER_REVENUE_RECOGNIZED,
        ).count()
        assert count == 1

    def test_revenue_recognized_different_walks_different_entries(self):
        db = _make_db()
        sub = _make_subscription(db)

        record_revenue_recognized_safe(db, sub, "walk-A")
        record_revenue_recognized_safe(db, sub, "walk-B")
        db.commit()

        count = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.event_type == LEDGER_REVENUE_RECOGNIZED,
        ).count()
        assert count == 2

    def test_grant_credits_on_payment_records_liability(self):
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, price=80.0)
        sub.credits_granted = False
        sub.credits_remaining = 0
        db.add(sub); db.commit()

        result = grant_credits_on_payment(db, sub, payment_id="pay-xyz")
        db.commit()

        assert result is True
        entries = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).all()
        assert len(entries) == 1
        assert entries[0].payment_id == "pay-xyz"

    def test_ledger_safe_never_raises(self, monkeypatch):
        """Falha no ledger não propaga exceção (best-effort)."""
        import app.services.credit_ledger_service as cls_mod

        def boom(*a, **k):
            raise RuntimeError("simulado")

        monkeypatch.setattr(cls_mod, "_entry_exists", boom)

        db = _make_db()
        sub = _make_subscription(db)

        # Não deve levantar
        record_liability_safe(db, sub)
        record_revenue_recognized_safe(db, sub, "walk-safe")

    def test_ledger_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("CREDIT_LEDGER_ENABLED", "false")
        db = _make_db()
        sub = _make_subscription(db)

        record_liability_safe(db, sub)
        record_revenue_recognized_safe(db, sub, "walk-off")
        db.commit()

        count = db.query(CreditLedgerEntry).count()
        assert count == 0


# ─── REGRESSÃO: crédito normal não muda ──────────────────────────────────────

class TestNoMoneyFlowRegression:
    """Garante que NENHUM fluxo de dinheiro ou crédito mudou."""

    def test_subscribe_credits_granted_immediately(self):
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4)
        sub = subscribe(db, _tenant(db), TUTOR_ID, plan.id)
        assert sub.credits_remaining == 4
        assert sub.credits_granted is True

    def test_consume_credit_decrements(self):
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4)
        subscribe(db, _tenant(db), TUTOR_ID, plan.id)
        sub = consume_credit_if_available(db, _tenant(db), TUTOR_ID)
        db.commit()
        assert sub is not None
        assert sub.credits_remaining == 3

    def test_refund_credit_restores(self):
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4)
        sub = subscribe(db, _tenant(db), TUTOR_ID, plan.id)
        consume_credit_if_available(db, _tenant(db), TUTOR_ID); db.commit()
        walk = Walk(
            id="walk-refund-reg", tutor_id=TUTOR_ID, tenant_id=TENANT_ID,
            pet_id=PET_ID,
            scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
            status="Agendado", subscription_id=sub.id, credit_refunded=False,
            created_at=datetime.utcnow(),
        )
        db.add(walk); db.commit()
        assert refund_credit_for_walk(db, walk) is True
        db.commit(); db.refresh(sub)
        assert sub.credits_remaining == 4

    def test_cancel_subscription_with_credits_does_not_move_money(self):
        """Cancelar assinatura com créditos registra breakage MAS não altera Payment."""
        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4)
        subscribe(db, _tenant(db), TUTOR_ID, plan.id)

        payment_count_before = db.query(Payment).count()
        cancel_subscription(db, TENANT_ID, TUTOR_ID)
        payment_count_after = db.query(Payment).count()

        assert payment_count_after == payment_count_before, "cancelamento não deve criar Payment"

    def test_sweep_does_not_create_payment(self):
        """sweep_expired_credits NÃO cria Payment (camada contábil pura)."""
        db = _make_db()
        _make_subscription(db, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)

        payment_count_before = db.query(Payment).count()
        sweep_expired_credits(db)
        payment_count_after = db.query(Payment).count()

        assert payment_count_after == payment_count_before

    def test_ledger_entry_not_payment(self):
        """CreditLedgerEntry existe mas não é Payment."""
        db = _make_db()
        plan = _make_plan(db)
        subscribe(db, _tenant(db), TUTOR_ID, plan.id)

        assert db.query(CreditLedgerEntry).count() >= 1
        # Payment não foi criado pelo subscribe (sem gateway)
        assert db.query(Payment).count() == 0


# ─── ITEM 3: endpoint sweep interno ──────────────────────────────────────────

class TestItem3SweepEndpoint:

    def _make_client(self, db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.routes import payments as payments_mod
        from app.core.database import get_db, get_global_db

        app_t = FastAPI()
        app_t.include_router(payments_mod.router)
        app_t.dependency_overrides[get_db] = lambda: db
        app_t.dependency_overrides[get_global_db] = lambda: db
        return TestClient(app_t)

    def test_sweep_endpoint_requires_token(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_SWEEP_TOKEN", "s3cr3t")
        db = _make_db()
        client = self._make_client(db)

        r = client.post("/payments/internal/credit-expiry/sweep",
                        headers={"x-internal-token": "wrong"})
        assert r.status_code == 401

    def test_sweep_endpoint_accepts_valid_token(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_SWEEP_TOKEN", "s3cr3t")
        db = _make_db()
        _make_subscription(db, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)
        client = self._make_client(db)

        r = client.post("/payments/internal/credit-expiry/sweep",
                        headers={"x-internal-token": "s3cr3t"})
        assert r.status_code == 200
        body = r.json()
        assert "recognized" in body

    def test_sweep_endpoint_idempotent(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_SWEEP_TOKEN", "s3cr3t")
        db = _make_db()
        _make_subscription(db, credits_remaining=2, status=SUBSCRIPTION_CANCELLED)
        client = self._make_client(db)

        r1 = client.post("/payments/internal/credit-expiry/sweep",
                         headers={"x-internal-token": "s3cr3t"})
        r2 = client.post("/payments/internal/credit-expiry/sweep",
                         headers={"x-internal-token": "s3cr3t"})

        assert r1.status_code == 200
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2.get("recognized") == 0  # segunda rodada não duplica


# ─── P1 + P3: ledger por ciclo de renovação ──────────────────────────────────

class TestP1CycleLiability:
    """P1: cada renovação mensal registra um NOVO passivo com cycle_reference distinto."""

    def test_renewal_registers_second_liability(self):
        """Ciclo 1 (subscribe) → 1 liability; após reset_credits_if_renewal → 2 liabilities com cycle_reference distintos.

        Estratégia: retro-data current_period_start do ciclo 1 para 30 dias atrás
        (simulando que a assinatura foi criada no mês passado). reset_credits_if_renewal
        avança current_period_start para hoje — datas distintas → cycle_reference distintos.
        """
        from app.services.recurring_plan_service import reset_credits_if_renewal

        db = _make_db()
        plan = _make_plan(db, walks_per_cycle=4, price=80.0)
        sub = subscribe(db, _tenant(db), TUTOR_ID, plan.id)
        db.commit()

        # Verifica que 1 liability já existe (do subscribe)
        entries_before = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).all()
        assert len(entries_before) == 1

        # Retro-data o cycle 1 para 30 dias atrás e atualiza o cycle_reference no
        # ledger para refletir isso — simula que a assinatura foi criada mês passado.
        past_start = datetime.utcnow() - timedelta(days=30)
        sub.current_period_start = past_start
        sub.current_period_end = datetime.utcnow() - timedelta(seconds=1)
        db.add(sub)
        existing_entry = entries_before[0]
        existing_entry.cycle_reference = past_start.date().isoformat()
        db.add(existing_entry)
        db.commit()

        cycle_ref_1 = existing_entry.cycle_reference

        # reset_credits_if_renewal avança current_period_start para agora (hoje)
        reset = reset_credits_if_renewal(db, sub)
        db.commit()
        assert reset is True

        entries_after = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).order_by(CreditLedgerEntry.created_at).all()
        assert len(entries_after) == 2, f"Esperado 2 liabilities, got {len(entries_after)}"
        cycle_refs = [e.cycle_reference for e in entries_after]
        assert cycle_refs[0] != cycle_refs[1], "cycle_reference deve ser distinto entre ciclos"
        # O 1º cycle_reference corresponde ao início do ciclo passado
        assert cycle_refs[0] == cycle_ref_1

    def test_cycle_idempotency_same_cycle(self):
        """record_liability_safe 2× no mesmo ciclo (mesma current_period_start) → apenas 1 linha."""
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, price=80.0)

        record_liability_safe(db, sub, payment_id=None)
        db.commit()
        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        count = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).count()
        assert count == 1

    def test_two_distinct_cycles_two_liabilities(self):
        """Avançar current_period_start manualmente e chamar record_liability_safe → 2 linhas."""
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, price=80.0)

        # Ciclo 1
        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        # Simula avanço do período (como reset_credits_if_renewal faria)
        sub.current_period_start = datetime.utcnow() + timedelta(days=30)
        db.add(sub); db.commit()

        # Ciclo 2
        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        count = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).count()
        assert count == 2, f"Esperado 2 liabilities (2 ciclos), got {count}"

    def test_best_effort_does_not_poison_session(self):
        """Após registrar liability e commit, o commit do caller continua funcionando."""
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, price=80.0)

        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        # A sessão deve seguir funcional — inserir outro objeto sem erro
        sub.credits_remaining = 2
        db.add(sub)
        db.commit()  # não deve levantar

        db.refresh(sub)
        assert sub.credits_remaining == 2


class TestP3GrossBase:
    """P3: unit_value e total_value usam preço BRUTO do plano, sem dedução de comissão."""

    def test_unit_value_is_gross_price_divided_by_walks(self):
        """unit_value = price / walks_per_cycle (bruto, sem qualquer dedução)."""
        db = _make_db()
        # price=100, 5 passeios → unit_value = 20.0
        sub = _make_subscription(db, walks_per_cycle=5, price=100.0)

        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        entry = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).first()
        assert entry is not None
        expected_unit = round(100.0 / 5, 4)
        expected_total = round(100.0, 2)
        assert abs(float(entry.unit_value) - expected_unit) < 0.0001, (
            f"unit_value={entry.unit_value} != {expected_unit} (deve ser bruto)"
        )
        assert abs(float(entry.total_value) - expected_total) < 0.01, (
            f"total_value={entry.total_value} != {expected_total} (deve ser bruto)"
        )

    def test_total_value_equals_plan_price(self):
        """total_value (credits_count × unit_value) deve fechar com o preço do plano."""
        db = _make_db()
        price = 129.90
        walks = 6
        sub = _make_subscription(db, walks_per_cycle=walks, price=price)

        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        entry = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).first()
        assert entry is not None
        unit = round(price / walks, 4)
        total_reconstructed = round(walks * unit, 2)
        assert abs(float(entry.total_value) - total_reconstructed) < 0.01

    def test_cycle_reference_set_on_liability(self):
        """cycle_reference é preenchido e tem formato YYYY-MM-DD."""
        import re
        db = _make_db()
        sub = _make_subscription(db, walks_per_cycle=4, price=80.0)

        record_liability_safe(db, sub, payment_id=None)
        db.commit()

        entry = db.query(CreditLedgerEntry).filter(
            CreditLedgerEntry.subscription_id == sub.id,
            CreditLedgerEntry.event_type == LEDGER_LIABILITY_CREATED,
        ).first()
        assert entry is not None
        assert entry.cycle_reference is not None
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", entry.cycle_reference), (
            f"cycle_reference '{entry.cycle_reference}' não é YYYY-MM-DD"
        )
