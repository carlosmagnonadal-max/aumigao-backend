"""Rede: finaliza passeio -> ledger 'a receber' -> apos payable -> 'disponivel' -> confirma Payment.walker_amount=0."""
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.payment import Payment
from app.models.walker_earning import WalkerEarning
from app.models.tenant_walker_access import TenantWalkerAccess
from app.routes.admin import _ensure_internal_walk_payment
from app.routes.walker import _balance_by_tenant


def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Tenant(id="t1", name="X", slug="x", status="active", plan="pro"))
    # full_name e nao 'name' (campo real do model User)
    db.add(User(id="k1", email="k1@x.com", full_name="K", role="walker", password_hash="x"))
    # walker_user_id e access_type="shared_network" (campos reais de TenantWalkerAccess)
    db.add(TenantWalkerAccess(
        id="twa1",
        tenant_id="t1",
        walker_user_id="k1",
        access_type="shared_network",
        status="active",
    ))
    db.commit()
    return db


def test_network_walk_flows_areceber_then_available():
    """Fluxo completo: finaliza passeio de rede -> ledger -> a receber -> disponivel.

    Tambem verifica que Payment.walker_amount == 0 (sem dupla contagem).
    """
    db = _db()

    # Walk de rede com pet_id e duration_minutes preenchidos (NOT NULL no model)
    w = Walk(
        id="w1",
        tenant_id="t1",
        tutor_id="tut",
        walker_id="k1",
        pet_id="pet-dummy",
        duration_minutes=30,
        price=50.0,
        status="Finalizado",
        scheduled_date="2026-06-10T10:00",
    )
    db.add(w)
    db.commit()

    # Finaliza -> cria WalkerEarning + Payment(walker_amount=0)
    _ensure_internal_walk_payment(w, db)
    db.commit()

    e = db.query(WalkerEarning).filter_by(walk_id="w1").one()

    # --- Fase "a receber": payable_at no futuro ---
    e.payable_at = datetime.now(timezone.utc) + timedelta(days=3)
    db.commit()

    # db.get e a forma correta no SQLAlchemy 2.x (evita aviso do .query().get())
    walker = db.get(User, "k1")
    by = _balance_by_tenant(walker, db)
    assert round(by["t1"]["pending"], 2) == round(e.amount, 2), (
        f"Esperado pending={e.amount:.2f}, got={by['t1']['pending']:.2f}"
    )
    assert by["t1"]["available"] == 0.0, (
        f"Disponivel deveria ser 0 enquanto payable_at no futuro, got={by['t1']['available']}"
    )

    # --- Fase "disponivel": payable_at no passado ---
    e.payable_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    walker2 = db.get(User, "k1")
    by2 = _balance_by_tenant(walker2, db)
    assert round(by2["t1"]["available"], 2) == round(e.amount, 2), (
        f"Esperado available={e.amount:.2f}, got={by2['t1']['available']:.2f}"
    )
    assert by2["t1"]["pending"] == 0.0, (
        f"Pendente deveria ser 0 apos payable_at passar, got={by2['t1']['pending']}"
    )

    # --- Sem dupla contagem: Payment do walk de rede tem walker_amount=0 ---
    pay = db.query(Payment).filter_by(walk_id="w1").one()
    assert (pay.walker_amount or 0) == 0, (
        f"Payment.walker_amount deveria ser 0 em passeio de rede, got={pay.walker_amount}"
    )
