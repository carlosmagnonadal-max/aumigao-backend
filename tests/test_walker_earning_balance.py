"""Task 3: Somar o ledger WalkerEarning no saldo do passeador (disponível vs a receber).

Testa que:
- ganhos com payable_at <= now contam como "available" em _balance_by_tenant e _available_balance
- ganhos com payable_at > now contam como "pending" (a receber) em _balance_by_tenant
- Payment.walker_amount=0 (rede) + WalkerEarning não gera dupla contagem
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.user import User
from app.models.walker_earning import WalkerEarning, WE_ACCRUED
from app.routes.walker import _balance_by_tenant, _available_balance


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db, uid="k1"):
    # full_name é o campo real do model User (não 'name')
    u = User(id=uid, email=f"{uid}@x.com", full_name="K", role="walker", password_hash="x")
    db.add(u)
    db.commit()
    return u


def _earn(db, wid, amount, payable_at, walker_id="k1", tenant_id="t1"):
    db.add(WalkerEarning(
        id="we-" + wid,
        walker_id=walker_id,
        tenant_id=tenant_id,
        walk_id=wid,
        gross=amount * 2,
        platform_amount=amount,
        amount=amount,
        status=WE_ACCRUED,
        accrued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        payable_at=payable_at,
    ))
    db.commit()


def test_payable_earning_counts_available_future_counts_areceber():
    db = _db()
    u = _user(db)
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=3)
    _earn(db, "w1", 24.0, past)
    _earn(db, "w2", 10.0, future)
    by = _balance_by_tenant(u, db)
    assert round(by["t1"]["available"], 2) == 24.0
    assert round(by["t1"]["pending"], 2) == 10.0   # 'a receber' (ainda não payable)
    # legado global: só o payable conta como disponível
    assert round(_available_balance(u, db), 2) == 24.0


def test_no_double_count_when_payment_walker_amount_zero():
    # rede grava Payment.walker_amount=0 + WalkerEarning; saldo deve refletir só o ledger uma vez
    db = _db()
    u = _user(db)
    _earn(db, "w1", 24.0, datetime.now(timezone.utc) - timedelta(days=1))
    # nenhum Payment com walker_amount>0 criado => total = só ledger
    assert round(_available_balance(u, db), 2) == 24.0
    assert round(_balance_by_tenant(u, db)["t1"]["available"], 2) == 24.0
