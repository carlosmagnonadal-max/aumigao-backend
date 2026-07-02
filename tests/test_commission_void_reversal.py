"""FIX 13 (P1) — estorno reverte a comissão do tenant (COMM_VOID).

- entrada ACCRUED (mês não faturado) -> COMM_VOID, não entra no faturamento.
- entrada BILLED/PAID (mês já cobrado) -> ajuste de crédito (amount<0) no período
  seguinte, que o faturamento mensal soma; o tenant é creditado.
- idempotente: reverter 2x não gera crédito dobrado.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.commission_entry import (
    CommissionEntry, COMM_ACCRUED, COMM_BILLED, COMM_PAID, COMM_VOID,
)
from app.models.tenant import Tenant
from app.services.commission_billing_service import (
    reverse_commission_for_walk, bill_tenant_commission,
)
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t-void"


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.commit()
    return db


def _entry(db, walk_id="w1", period="2026-06", amount=8.0, status=COMM_ACCRUED, asaas_id=None):
    e = CommissionEntry(
        id="ce-" + walk_id, tenant_id=TENANT_ID, walk_id=walk_id, period=period,
        walk_price=100.0, commission_percent=8.0, amount=amount, is_network=False,
        status=status, asaas_payment_id=asaas_id,
    )
    db.add(e); db.commit()
    return e


def test_accrued_commission_voided_not_billed():
    db = _db()
    _entry(db, "w1", period="2026-06", amount=8.0, status=COMM_ACCRUED)
    out = reverse_commission_for_walk(db, "w1", reason="chargeback")
    db.commit()
    assert out.status == COMM_VOID
    # Faturamento do mês não inclui a entrada void.
    charged = {}
    def charge_fn(db, tenant, total, period, desc):
        charged["total"] = total
        return "asaas-x"
    result = bill_tenant_commission(db, TENANT_ID, "2026-06", charge_fn=charge_fn)
    assert result is None  # nada a faturar (única entrada foi anulada)


def test_billed_commission_generates_credit_next_period():
    db = _db()
    _entry(db, "w1", period="2026-06", amount=8.0, status=COMM_BILLED, asaas_id="asaas-jun")
    out = reverse_commission_for_walk(db, "w1", reason="chargeback")
    db.commit()
    # Original permanece billed (não apaga histórico); ajuste negativo criado no mês seguinte.
    orig = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    assert orig.status == COMM_BILLED
    adj = db.query(CommissionEntry).filter_by(walk_id="comm-void-adj-w1").one()
    assert adj.status == COMM_ACCRUED
    assert adj.period == "2026-07"
    assert adj.amount == -8.0

    # No faturamento de julho, o crédito reduz o total. Adiciona um passeio positivo
    # de 20 no mesmo mês para provar a soma (20 - 8 = 12).
    _entry(db, "w2", period="2026-07", amount=20.0, status=COMM_ACCRUED)
    seen = {}
    def charge_fn(db, tenant, total, period, desc):
        seen["total"] = total
        return "asaas-jul"
    bill_tenant_commission(db, TENANT_ID, "2026-07", charge_fn=charge_fn)
    assert seen["total"] == 12.0


def test_reversal_is_idempotent():
    db = _db()
    _entry(db, "w1", period="2026-06", amount=8.0, status=COMM_PAID, asaas_id="asaas-jun")
    reverse_commission_for_walk(db, "w1", reason="chargeback"); db.commit()
    reverse_commission_for_walk(db, "w1", reason="chargeback"); db.commit()
    # Só UM ajuste, não dois.
    adjustments = db.query(CommissionEntry).filter_by(walk_id="comm-void-adj-w1").all()
    assert len(adjustments) == 1
    assert adjustments[0].amount == -8.0


def test_reversal_noop_when_no_entry():
    db = _db()
    assert reverse_commission_for_walk(db, "inexistente", reason="x") is None
