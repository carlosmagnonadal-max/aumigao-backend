# backend/tests/test_commission_billing.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import CommissionEntry, COMM_ACCRUED

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

class _Walk:
    def __init__(self, id, tenant_id, walker_id, price, status="Finalizado"):
        self.id = id; self.tenant_id = tenant_id; self.walker_id = walker_id
        self.assigned_walker_id = None; self.price = price; self.status = status

def test_accrue_creates_entry_for_own_walker():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w1", "t1", "k1", 30.0)
    split = {"commission_percent": 10.0, "platform_amount": 3.0, "walker_amount": 27.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06")
    db.commit()
    e = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    assert e.amount == 3.0 and e.commission_percent == 10.0
    assert e.status == COMM_ACCRUED and e.is_network is False

def test_accrue_is_idempotent():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w1", "t1", "k1", 30.0)
    split = {"commission_percent": 10.0, "platform_amount": 3.0, "walker_amount": 27.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w1").count() == 1

def test_accrue_skips_network_walk():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w2", "t1", "k1", 30.0)
    split = {"commission_percent": 18.0, "platform_amount": 5.4, "walker_amount": 24.6}
    accrue_commission_for_walk(db, walk, split, is_network=True, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w2").count() == 0

def test_accrue_skips_zero_price():
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w3", "t1", "k1", 0.0)
    split = {"commission_percent": 10.0, "platform_amount": 0.0, "walker_amount": 0.0}
    accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06"); db.commit()
    assert db.query(CommissionEntry).filter_by(walk_id="w3").count() == 0

def test_accrue_skips_walk_without_tenant_id():
    """Walk com tenant_id=None (is_network=False, price>0) não deve criar CommissionEntry."""
    from app.services.commission_billing_service import accrue_commission_for_walk
    db = _db()
    walk = _Walk("w4", None, "k1", 30.0)
    split = {"commission_percent": 10.0, "platform_amount": 3.0, "walker_amount": 27.0}
    result = accrue_commission_for_walk(db, walk, split, is_network=False, period="2026-06")
    db.commit()
    assert result is None
    assert db.query(CommissionEntry).filter_by(walk_id="w4").count() == 0


# ---------------------------------------------------------------------------
# Task 5: faturamento mensal
# ---------------------------------------------------------------------------
from datetime import datetime, timezone
from app.models.tenant import Tenant
from app.models.commission_entry import COMM_BILLED, COMM_PAID


def _db_with_tenant():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro",
                  document_number="11222333000181", contact_email="fin@x.com"))
    db.commit()
    return db

def _seed_entry(db, walk_id, amount, period="2026-06", status="accrued", tenant_id="t1"):
    from app.models.commission_entry import CommissionEntry
    db.add(CommissionEntry(id="ce-" + walk_id, tenant_id=tenant_id, walk_id=walk_id,
                           period=period, walk_price=amount * 10, commission_percent=10.0,
                           amount=amount, is_network=False, status=status))
    db.commit()

def test_bill_aggregates_accrued_and_marks_billed():
    from app.services.commission_billing_service import bill_tenant_commission
    db = _db_with_tenant()
    _seed_entry(db, "w1", 3.0); _seed_entry(db, "w2", 4.5)
    captured = {}
    def fake_charge(db_, tenant, total, period, description):
        captured.update(total=total, period=period, tenant=tenant.id)
        return "asaas-charge-1"
    charge = bill_tenant_commission(db, "t1", "2026-06", charge_fn=fake_charge)
    db.commit()
    assert captured["total"] == 7.5
    assert charge == "asaas-charge-1"
    from app.models.commission_entry import CommissionEntry
    rows = db.query(CommissionEntry).filter_by(tenant_id="t1", period="2026-06").all()
    assert all(r.status == COMM_BILLED and r.asaas_payment_id == "asaas-charge-1" for r in rows)

def test_bill_noop_when_nothing_accrued():
    from app.services.commission_billing_service import bill_tenant_commission
    db = _db_with_tenant()
    called = {"n": 0}
    def fake_charge(*a, **k):
        called["n"] += 1; return "x"
    assert bill_tenant_commission(db, "t1", "2026-06", charge_fn=fake_charge) is None
    assert called["n"] == 0

def test_bill_ignores_already_billed():
    from app.services.commission_billing_service import bill_tenant_commission
    db = _db_with_tenant()
    _seed_entry(db, "w1", 3.0, status="billed")
    def fake_charge(*a, **k):
        raise AssertionError("não deveria cobrar — já faturado")
    assert bill_tenant_commission(db, "t1", "2026-06", charge_fn=fake_charge) is None

def test_run_monthly_bills_each_tenant_with_accrued():
    from app.services.commission_billing_service import run_monthly_commission_billing
    db = _db_with_tenant()
    db.add(Tenant(id="t2", name="Y", slug="y", status="active", plan="enterprise",
                  document_number="99888777000166", contact_email="fin@y.com")); db.commit()
    _seed_entry(db, "w1", 3.0, tenant_id="t1")
    _seed_entry(db, "w2", 5.0, tenant_id="t2")
    billed = []
    def fake_charge(db_, tenant, total, period, description):
        billed.append((tenant.id, total)); return "c-" + tenant.id
    run_monthly_commission_billing(db, "2026-06", charge_fn=fake_charge)
    db.commit()
    assert sorted(billed) == [("t1", 3.0), ("t2", 5.0)]


def test_run_monthly_partial_failure_isolates_per_tenant():
    """Falha no charge_fn de t2 NÃO deve desfazer o billed de t1.

    Garante que run_monthly_commission_billing commita cada tenant
    individualmente: se t2 falhar, t1 permanece persistido como 'billed'
    e o retorno contém apenas o charge id de t1.
    """
    from app.services.commission_billing_service import run_monthly_commission_billing

    db = _db_with_tenant()
    db.add(Tenant(id="t2", name="Y", slug="y", status="active", plan="enterprise",
                  document_number="99888777000166", contact_email="fin@y.com"))
    db.commit()

    _seed_entry(db, "w1", 3.0, tenant_id="t1")
    _seed_entry(db, "w2", 5.0, tenant_id="t2")

    def fake_charge(db_, tenant, total, period, description):
        if tenant.id == "t2":
            raise RuntimeError("asaas down")
        return "c-t1"

    result = run_monthly_commission_billing(db, "2026-06", charge_fn=fake_charge)

    # Retorno: só o charge de t1
    assert result == ["c-t1"]

    # Forçar reload do banco (descarta cache da sessão)
    db.expire_all()

    t1_entries = db.query(CommissionEntry).filter_by(tenant_id="t1", period="2026-06").all()
    t2_entries = db.query(CommissionEntry).filter_by(tenant_id="t2", period="2026-06").all()

    # t1 deve estar billed e persistido
    assert all(e.status == COMM_BILLED for e in t1_entries), \
        f"t1 deveria estar billed mas está: {[e.status for e in t1_entries]}"

    # t2 deve continuar accrued (não cobrado)
    assert all(e.status == COMM_ACCRUED for e in t2_entries), \
        f"t2 deveria continuar accrued mas está: {[e.status for e in t2_entries]}"


# ---------------------------------------------------------------------------
# Item 1: Pré-check anti-cobrança-dupla em bill_tenant_commission
# ---------------------------------------------------------------------------

def test_bill_returns_existing_id_and_skips_charge_when_already_billed():
    """Re-execução sobre entrada já billed deve retornar o asaas_payment_id
    existente SEM chamar charge_fn (proteção contra cobrança dupla em
    reprocessamentos parciais multi-tenant).
    """
    from app.services.commission_billing_service import bill_tenant_commission

    db = _db_with_tenant()
    # Uma entrada já billed (com asaas_payment_id setado) para t1/2026-06
    _seed_entry(db, "w1", 3.0, status="billed", tenant_id="t1")
    existing_entry = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    existing_entry.asaas_payment_id = "existing-charge-999"
    db.commit()

    # Uma entrada accrued adicional no mesmo t1/period
    _seed_entry(db, "w2", 4.0, status="accrued", tenant_id="t1")

    def bomb_charge(*a, **k):
        raise AssertionError("charge_fn NÃO deveria ser chamado — já existe cobrança billed")

    result = bill_tenant_commission(db, "t1", "2026-06", charge_fn=bomb_charge)
    assert result == "existing-charge-999", (
        f"Esperado 'existing-charge-999' (id existente), got {result!r}"
    )
