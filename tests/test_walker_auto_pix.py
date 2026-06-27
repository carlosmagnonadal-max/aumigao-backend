# backend/tests/test_walker_auto_pix.py
"""Task 3 — PIX automático na aprovação do saque (gated/OFF por padrão).

Cenários:
- flag OFF => no-op (nenhuma transferência feita)
- flag ON + chave PIX => transfere 1× e é idempotente (não transfere de novo)
- flag ON + sem chave PIX => HTTPException 400
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401 - registra todas as tabelas
from app.core.database import Base
from app.models.payment import Payment
from app.models.walker_profile import WalkerProfile
from app.models.user import User


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed_withdrawal(db, pid="wd1", walker_id="k1", amount=50.0):
    db.add(User(id=walker_id, email="k@x.com", full_name="K", role="walker", password_hash="x"))
    db.add(WalkerProfile(id="wp1", user_id=walker_id, pix_key="k@pix.com"))
    db.add(Payment(id=pid, tenant_id="t1", tutor_id=walker_id, walk_id=None,
                   amount=-amount, status="pending", provider="pix"))
    db.commit()


def test_flag_off_does_not_transfer(monkeypatch):
    from app.services import walker_payout_service as svc
    monkeypatch.setenv("WALKER_AUTO_PIX_ENABLED", "false")
    db = _db()
    _seed_withdrawal(db)
    called = {"n": 0}
    monkeypatch.setattr(
        svc, "_asaas_transfer_post",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "tr1"
    )
    out = svc.transfer_to_walker(db, db.get(Payment, "wd1"))
    assert out is None and called["n"] == 0  # flag off => no-op


def test_flag_on_transfers_once_and_is_idempotent(monkeypatch):
    from app.services import walker_payout_service as svc
    monkeypatch.setenv("WALKER_AUTO_PIX_ENABLED", "true")
    db = _db()
    _seed_withdrawal(db)
    calls = {"n": 0}

    def _mock_transfer(value, pix_key):
        calls["n"] += 1
        return "tr-123"

    monkeypatch.setattr(svc, "_asaas_transfer_post", _mock_transfer)
    p = db.get(Payment, "wd1")
    tid = svc.transfer_to_walker(db, p)
    db.commit()
    assert tid == "tr-123"
    assert p.provider_payment_id == "tr-123"
    assert calls["n"] == 1

    # Idempotente: já transferido => não chama de novo
    tid2 = svc.transfer_to_walker(db, db.get(Payment, "wd1"))
    assert tid2 == "tr-123"
    assert calls["n"] == 1  # sem chamada adicional


def test_flag_on_missing_pix_key_raises(monkeypatch):
    import pytest
    from fastapi import HTTPException
    from app.services import walker_payout_service as svc
    monkeypatch.setenv("WALKER_AUTO_PIX_ENABLED", "true")
    db = _db()
    db.add(User(id="k2", email="k2@x.com", full_name="K2", role="walker", password_hash="x"))
    db.add(WalkerProfile(id="wp2", user_id="k2", pix_key=None))
    db.add(Payment(id="wd2", tenant_id="t1", tutor_id="k2", walk_id=None,
                   amount=-50, status="pending", provider="pix"))
    db.commit()
    with pytest.raises(HTTPException) as exc_info:
        svc.transfer_to_walker(db, db.get(Payment, "wd2"))
    assert exc_info.value.status_code == 400
