"""Task 4 + Correção 2: Bug fix — dedução de saque nas duas funções de saldo.

Regra correta: saques em {pending, paid} REDUZEM o saldo;
               saques em {rejected} NÃO reduzem.

'approved' removido do conjunto: admin.py/approve_withdrawal grava status='paid'
diretamente — 'approved' nunca foi gravado em Payment de saque (status morto).

Bugs corrigidos:
- _available_balance: só descontava {pending} — 'paid' ficava de fora
  (double-spend: passeador poderia sacar, admin aprovava→paid, saldo voltava inteiro).
- _balance_by_tenant: descontava TODOS os status (incluindo 'rejected')
  (saldo menor que o correto quando saque era recusado).
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.user import User
from app.models.pet import Pet
from app.models.walk import Walk
from app.models.payment import Payment
from app.routes.walker import _balance_by_tenant, _available_balance

WALKER_ID = "k1"
TUTOR_ID = "tut1"
PET_ID = "pet1"
TENANT_ID = "t1"


def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    # Seed mínimo: walker + tutor + pet (Walk exige pet_id NOT NULL)
    db.add(User(id=WALKER_ID, email="k1@x.com", full_name="K", role="walker", password_hash="x"))
    db.add(User(id=TUTOR_ID, email="tut@x.com", full_name="T", role="tutor", password_hash="x"))
    db.add(Pet(id=PET_ID, name="Rex", species="cachorro", tutor_id=TUTOR_ID, tenant_id=TENANT_ID))
    db.commit()
    return db


def _user(db):
    return db.get(User, WALKER_ID)


def _credit(db, wid, amount, tenant_id=TENANT_ID):
    """Cria um Walk finalizado + Payment pago com walker_amount = amount."""
    db.add(Walk(
        id=wid,
        tenant_id=tenant_id,
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        pet_id=PET_ID,
        price=amount,
        status="Finalizado",
        scheduled_date="2026-06-10T10:00",
        duration_minutes=30,
    ))
    db.add(Payment(
        id="p-" + wid,
        tenant_id=tenant_id,
        tutor_id=TUTOR_ID,
        walk_id=wid,
        amount=amount,
        status="paid",
        provider="internal",
        walker_amount=amount,
    ))
    db.commit()


def _withdrawal(db, pid, amount, status, tenant_id=TENANT_ID):
    """Cria um Payment de saque (provider='pix', walk_id=None, amount negativo).
    tutor_id é preenchido com WALKER_ID pois _balance_by_tenant e _available_balance
    identificam o saque do walker via Payment.tutor_id == user.id.
    """
    db.add(Payment(
        id=pid,
        tenant_id=tenant_id,
        tutor_id=WALKER_ID,
        walk_id=None,
        amount=-amount,
        status=status,
        provider="pix",
    ))
    db.commit()


# ─── Testes _available_balance ────────────────────────────────────────────────

def test_paid_withdrawal_deducts_available_balance():
    """Saque com status='paid' DEVE reduzir _available_balance (bug: antes não reduzia)."""
    db = _db()
    u = _user(db)
    _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 40.0, "paid")
    result = round(_available_balance(u, db), 2)
    assert result == 60.0, f"Esperado 60.0, got {result} (bug: 'paid' não descontava)"


def test_rejected_withdrawal_does_not_deduct_available_balance():
    """Saque com status='rejected' NÃO deve reduzir _available_balance."""
    db = _db()
    u = _user(db)
    _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 40.0, "rejected")
    result = round(_available_balance(u, db), 2)
    assert result == 100.0, f"Esperado 100.0, got {result} (saque rejeitado não devia descontar)"


def test_pending_and_paid_deduct_available_balance():
    """Saques com status='pending' e 'paid' DEVEM reduzir _available_balance.

    Nota: 'approved' foi removido de _WITHDRAWAL_DEDUCT_STATUSES porque
    admin.py/approve_withdrawal grava status='paid' diretamente — 'approved'
    nunca é gravado em Payment de saque e era um status morto. O teste anterior
    usava 'approved' explicitamente; corrigido para 'paid' (status real pós-aprovação).
    """
    db = _db()
    u = _user(db)
    _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 10.0, "pending")
    _withdrawal(db, "wd2", 15.0, "paid")
    result = round(_available_balance(u, db), 2)
    assert result == 75.0, f"Esperado 75.0, got {result}"


# ─── Testes _balance_by_tenant ────────────────────────────────────────────────

def test_paid_withdrawal_deducts_balance_by_tenant():
    """Saque com status='paid' DEVE reduzir _balance_by_tenant[t1]['available']."""
    db = _db()
    u = _user(db)
    _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 40.0, "paid")
    by = _balance_by_tenant(u, db)
    result = round(by[TENANT_ID]["available"], 2)
    assert result == 60.0, f"Esperado 60.0, got {result} (bug: 'paid' não descontava)"


def test_rejected_withdrawal_does_not_deduct_balance_by_tenant():
    """Saque com status='rejected' NÃO deve reduzir _balance_by_tenant (bug: antes reduzia)."""
    db = _db()
    u = _user(db)
    _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 40.0, "rejected")
    by = _balance_by_tenant(u, db)
    result = round(by[TENANT_ID]["available"], 2)
    assert result == 100.0, f"Esperado 100.0, got {result} (bug: 'rejected' descontava indevidamente)"


def test_pending_and_paid_deduct_balance_by_tenant():
    """Saques com status='pending' e 'paid' DEVEM reduzir _balance_by_tenant.

    Nota: 'approved' foi removido de _WITHDRAWAL_DEDUCT_STATUSES porque
    admin.py/approve_withdrawal grava status='paid' diretamente — 'approved'
    nunca é gravado em Payment de saque e era um status morto. O teste anterior
    usava 'approved' explicitamente; corrigido para 'paid' (status real pós-aprovação).
    """
    db = _db()
    u = _user(db)
    _credit(db, "w1", 100.0)
    _withdrawal(db, "wd1", 10.0, "pending")
    _withdrawal(db, "wd2", 15.0, "paid")
    by = _balance_by_tenant(u, db)
    result = round(by[TENANT_ID]["available"], 2)
    assert result == 75.0, f"Esperado 75.0, got {result}"
