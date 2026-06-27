"""TDD — Correção 1: fallback per-walk no _available_balance.

Problema:
    A Fase 2 grava Payment.walker_amount = 0.0 (NOT NULL) para passeios de REDE.
    Antes da correção, `payments_with_split` ficava não-vazio quando existia qualquer
    Payment com walker_amount (mesmo 0.0), e o fallback all-or-nothing nunca rodava —
    passeios LEGADOS com walker_amount IS NULL (pré-split) eram silenciosamente ignorados.

Correção:
    Fallback POR PAGAMENTO/PASSEIO:
    - gross = Σ walker_amount de Payments pagos onde walker_amount IS NOT NULL (split real)
            + Σ walk.price dos passeios concluídos SEM Payment pago com walker_amount (legado)

Cenários testados:
    1. Só legado (walker_amount IS NULL)  → gross = Σ walk.price
    2. Só split (walker_amount preenchido) → gross = Σ walker_amount
    3. Misto legado + rede (0.0)           → gross = Σ walker_amount(0.0 da rede) + Σ walk.price(legados)
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.payment import Payment
from app.models.pet import Pet
from app.models.user import User
from app.models.walk import Walk
from app.routes.walker import _available_balance

WALKER_ID = "w-fb"
TUTOR_ID  = "t-fb"
PET_ID    = "p-fb"
TENANT_ID = "ten-fb"


def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(User(id=WALKER_ID, email="w@fb.com", full_name="Walker FB", role="walker", password_hash="x"))
    db.add(User(id=TUTOR_ID,  email="t@fb.com", full_name="Tutor FB",  role="cliente", password_hash="x"))
    db.add(Pet(id=PET_ID, name="Bolt", species="cachorro", tutor_id=TUTOR_ID, tenant_id=TENANT_ID))
    db.commit()
    return db


def _user(db):
    return db.get(User, WALKER_ID)


def _legacy_walk(db, walk_id: str, price: float):
    """Walk concluído SEM Payment com walker_amount (pré-split / legado)."""
    db.add(Walk(
        id=walk_id,
        tenant_id=TENANT_ID,
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        pet_id=PET_ID,
        price=price,
        status="Finalizado",
        scheduled_date="2026-06-10T10:00",
        duration_minutes=30,
    ))
    db.commit()


def _split_walk(db, walk_id: str, price: float, walker_amount: float):
    """Walk concluído COM Payment pago e walker_amount (split calculado, pode ser 0.0)."""
    db.add(Walk(
        id=walk_id,
        tenant_id=TENANT_ID,
        tutor_id=TUTOR_ID,
        walker_id=WALKER_ID,
        pet_id=PET_ID,
        price=price,
        status="Finalizado",
        scheduled_date="2026-06-10T10:00",
        duration_minutes=30,
    ))
    db.add(Payment(
        id="pay-" + walk_id,
        tenant_id=TENANT_ID,
        tutor_id=TUTOR_ID,
        walk_id=walk_id,
        amount=price,
        status="paid",
        provider="internal",
        walker_amount=walker_amount,
    ))
    db.commit()


# ─── Cenário 1: Só legado (walker_amount IS NULL) ─────────────────────────────

def test_only_legacy_walks_uses_walk_price():
    """Walker só com passeios legados (sem Payment) → gross = Σ walk.price."""
    db = _db()
    u = _user(db)

    _legacy_walk(db, "wleg1", 50.0)
    _legacy_walk(db, "wleg2", 30.0)

    result = round(_available_balance(u, db), 2)
    assert result == 80.0, (
        f"Esperado 80.0 (50+30 via fallback walk.price), got {result}"
    )


# ─── Cenário 2: Só split (walker_amount preenchido) ──────────────────────────

def test_only_split_walks_uses_walker_amount():
    """Walker só com passeios com split → gross = Σ walker_amount."""
    db = _db()
    u = _user(db)

    _split_walk(db, "wspl1", price=100.0, walker_amount=85.0)
    _split_walk(db, "wspl2", price=60.0,  walker_amount=51.0)

    result = round(_available_balance(u, db), 2)
    assert result == 136.0, (
        f"Esperado 136.0 (85+51 via walker_amount), got {result}"
    )


# ─── Cenário 3: Misto — legado (NULL) + rede (0.0) ───────────────────────────

def test_mixed_legacy_and_network_no_double_count():
    """Walker com passeio legado (NULL) + passeio de rede (walker_amount=0.0).

    Antes da correção (all-or-nothing): payments_with_split estava não-vazio
    (passeio de rede com walker_amount=0.0), fallback nunca rodava, passeio
    legado era silenciosamente ignorado → gross = 0.0 (errado).

    Após a correção (per-walk): gross = walker_amount(0.0) + walk.price(legado=50.0).
    """
    db = _db()
    u = _user(db)

    # Passeio de rede: Payment com walker_amount=0.0 (REDE — ganho vai pro ledger)
    _split_walk(db, "wnet1", price=70.0, walker_amount=0.0)

    # Passeio legado pré-split: sem Payment com walker_amount
    _legacy_walk(db, "wleg1", price=50.0)

    result = round(_available_balance(u, db), 2)
    # walker_amount da rede = 0.0 (legítimo, não descartado)
    # walk.price do legado = 50.0 (capturado pelo fallback per-walk)
    assert result == 50.0, (
        f"Esperado 50.0 (0.0 da rede + 50.0 do legado via fallback per-walk), got {result}. "
        "Se got 0.0: bug all-or-nothing não corrigido (legado ignorado). "
        "Se got 120.0: dupla contagem (legado E price do walk de rede somados)."
    )


# ─── Cenário 4: Rede com split real (walker_amount > 0) + legado ─────────────

def test_real_split_and_legacy_no_double_count():
    """Walker com split real (walker_amount=42.0) + legado (walk.price=50.0).

    O passeio com split NÃO deve entrar também pelo fallback de walk.price.
    gross deve ser 42.0 + 50.0 = 92.0, não 42.0 + 50.0 + 100.0 (price do split).
    """
    db = _db()
    u = _user(db)

    # Passeio com split real
    _split_walk(db, "wspl1", price=100.0, walker_amount=42.0)

    # Passeio legado pré-split
    _legacy_walk(db, "wleg1", price=50.0)

    result = round(_available_balance(u, db), 2)
    assert result == 92.0, (
        f"Esperado 92.0 (42.0 de split + 50.0 de legado), got {result}. "
        "Se got 192.0: dupla contagem (split walk entrando também no fallback)."
    )
