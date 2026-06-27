"""Fluxo completo: finaliza 2 passeios próprios + 1 de rede → acumula só os 2 próprios
→ fatura mensal agrega num único charge → webhook marca pago."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.walk import Walk
from app.models.commission_entry import CommissionEntry, COMM_PAID
from app.routes.admin import _ensure_internal_walk_payment
from app.services.commission_billing_service import (
    bill_tenant_commission, mark_commission_paid,
)


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro",
                  document_number="11222333000181", contact_email="fin@x.com"))
    db.commit()
    return db


def test_full_flow_two_own_walks_billed_then_paid():
    """2 passeios próprios acumulam → faturamento agrega num charge → webhook paga tudo."""
    db = _db()

    # Cria e finaliza 2 passeios próprios (walker_id "k1" = passeador próprio do tenant)
    for wid in ("w1", "w2"):
        w = Walk(
            id=wid,
            tenant_id="t1",
            tutor_id="tut",
            walker_id="k1",
            pet_id="pet-dummy",
            scheduled_date="2026-06-15",
            duration_minutes=30,
            price=50.0,
            status="Finalizado",
        )
        db.add(w)
        db.commit()
        _ensure_internal_walk_payment(w, db)
        db.commit()

    # period é derivado da entrada criada (robusto ao campo de data usado internamente)
    period = db.query(CommissionEntry).first().period

    # Deve ter exatamente 2 entradas (uma por passeio próprio)
    entries_before = db.query(CommissionEntry).filter_by(tenant_id="t1").all()
    assert len(entries_before) == 2, f"Esperado 2 entradas, got {len(entries_before)}"

    # Faturamento mensal: agrega as 2 entradas num único charge
    cid = bill_tenant_commission(db, "t1", period, charge_fn=lambda *a, **k: "charge-1")
    db.commit()
    assert cid == "charge-1"

    # Webhook de pagamento: marca as 2 entradas como pago
    paid_count = mark_commission_paid(db, "charge-1")
    db.commit()
    assert paid_count == 2

    # Verifica o estado final
    rows = db.query(CommissionEntry).filter_by(tenant_id="t1").all()
    assert len(rows) == 2
    assert all(r.status == COMM_PAID for r in rows)
    # 2 × (50.0 × 10%) = 10.0
    assert round(sum(r.amount for r in rows), 2) == 10.0


def test_network_walk_does_not_accrue():
    """Passeio de REDE não deve gerar CommissionEntry (margem capturada no crédito — Fase 2)."""
    from app.models.tenant_walker_access import TenantWalkerAccess

    db = _db()

    # Registra walker "k-net" como passeador DE REDE do tenant (access_type = shared_network)
    db.add(TenantWalkerAccess(
        id="twa-1",
        tenant_id="t1",
        walker_user_id="k-net",
        access_type="shared_network",
        status="active",
    ))
    db.commit()

    w = Walk(
        id="w-net",
        tenant_id="t1",
        tutor_id="tut",
        walker_id="k-net",
        pet_id="pet-dummy",
        scheduled_date="2026-06-15",
        duration_minutes=30,
        price=50.0,
        status="Finalizado",
    )
    db.add(w)
    db.commit()
    _ensure_internal_walk_payment(w, db)
    db.commit()

    # Passeio de rede NÃO deve gerar entrada
    count = db.query(CommissionEntry).filter_by(walk_id="w-net").count()
    assert count == 0, f"Passeio de rede gerou {count} entrada(s) indevida(s)"


def test_own_walk_accrues_network_walk_does_not():
    """2 próprios + 1 de rede → só os 2 próprios acumulam; fatura só os 2."""
    from app.models.tenant_walker_access import TenantWalkerAccess

    db = _db()

    # Registra walker de rede
    db.add(TenantWalkerAccess(
        id="twa-2",
        tenant_id="t1",
        walker_user_id="k-net",
        access_type="shared_network",
        status="active",
    ))
    db.commit()

    # 2 passeios próprios
    for wid in ("own-1", "own-2"):
        w = Walk(
            id=wid,
            tenant_id="t1",
            tutor_id="tut",
            walker_id="k1",
            pet_id="pet-dummy",
            scheduled_date="2026-06-20",
            duration_minutes=30,
            price=40.0,
            status="Finalizado",
        )
        db.add(w)
        db.commit()
        _ensure_internal_walk_payment(w, db)
        db.commit()

    # 1 passeio de rede (não deve acumular)
    w_net = Walk(
        id="net-1",
        tenant_id="t1",
        tutor_id="tut",
        walker_id="k-net",
        pet_id="pet-dummy",
        scheduled_date="2026-06-20",
        duration_minutes=30,
        price=40.0,
        status="Finalizado",
    )
    db.add(w_net)
    db.commit()
    _ensure_internal_walk_payment(w_net, db)
    db.commit()

    # Apenas os 2 próprios acumularam
    all_entries = db.query(CommissionEntry).filter_by(tenant_id="t1").all()
    assert len(all_entries) == 2, f"Esperado 2 entradas (próprios), got {len(all_entries)}"
    walk_ids_accrued = {e.walk_id for e in all_entries}
    assert "own-1" in walk_ids_accrued
    assert "own-2" in walk_ids_accrued
    assert "net-1" not in walk_ids_accrued

    # Fatura os 2 próprios num único charge
    period = all_entries[0].period
    cid = bill_tenant_commission(db, "t1", period, charge_fn=lambda *a, **k: "charge-2")
    db.commit()
    assert cid == "charge-2"

    # Webhook marca como pago
    assert mark_commission_paid(db, "charge-2") == 2
    db.commit()

    rows = db.query(CommissionEntry).filter_by(tenant_id="t1").all()
    assert all(r.status == COMM_PAID for r in rows)
    # 2 × (40.0 × 10%) = 8.0
    assert round(sum(r.amount for r in rows), 2) == 8.0
