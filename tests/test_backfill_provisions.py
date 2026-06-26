"""Testa a função backfill_provisions (pura, in-memory, idempotente)."""
import sys
from pathlib import Path
# Garante que o diretório raiz do backend está no sys.path para importar scripts.*
_backend_root = str(Path(__file__).resolve().parents[1])
if _backend_root not in sys.path:
    sys.path.insert(0, _backend_root)

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 — garante tabelas no Base.metadata
from app.core.database import Base
from app.models.fiscal import PaymentProvision
from app.models.payment import Payment
from app.models.tenant import Tenant
from app.models.user import User

_CONFIRMED = "pagamento_confirmado_sandbox"


def _db():
    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    return sessionmaker(bind=e)()


def _seed(db, n_confirmed=3, n_other=2):
    """Cria n_confirmed pagamentos confirmados e n_other em outros status."""
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="pro"))
    db.add(User(id="u1", email="u@test.com", password_hash="x", role="tutor"))
    db.commit()

    for i in range(n_confirmed):
        db.add(Payment(
            id=f"pay-c{i}", tenant_id="t1", tutor_id="u1",
            amount=100.0, status=_CONFIRMED, provider="asaas_sandbox",
        ))
    for i in range(n_other):
        db.add(Payment(
            id=f"pay-o{i}", tenant_id="t1", tutor_id="u1",
            amount=50.0, status="aguardando_pagamento", provider="asaas_sandbox",
        ))
    db.commit()


def test_backfill_creates_provisions_for_confirmed():
    from scripts.backfill_provisions import backfill_provisions
    db = _db()
    _seed(db, n_confirmed=3)
    n = backfill_provisions(db)
    assert n == 3
    assert db.query(PaymentProvision).count() == 3


def test_backfill_idempotent_second_run():
    from scripts.backfill_provisions import backfill_provisions
    db = _db()
    _seed(db, n_confirmed=2)
    backfill_provisions(db)
    n2 = backfill_provisions(db)
    # Segunda rodada: compute_and_store_provision retorna existente, n continua incrementando
    # mas não duplica registros
    assert db.query(PaymentProvision).count() == 2


def test_backfill_skips_non_confirmed():
    from scripts.backfill_provisions import backfill_provisions
    db = _db()
    _seed(db, n_confirmed=1, n_other=5)
    n = backfill_provisions(db)
    assert n == 1
    assert db.query(PaymentProvision).count() == 1
