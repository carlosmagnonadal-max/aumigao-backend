"""FIX 11 (P2) — void de earning após PIX pago emite alerta + notificação ao admin
("PIX a recuperar"). O void NÃO é bloqueado.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.walker_earning import WalkerEarning, WE_ACCRUED, WE_VOID
from app.models.payment import Payment
from app.models.user import User
from app.models.tenant import Tenant
from app.models.notification import Notification
from app.services.walker_payout_service import void_walker_earning
from app.services.tenant_seed_service import DEFAULT_TENANT_SLUG

TENANT_ID = "t1"
WALKER_ID = "k1"


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id=TENANT_ID, name="Aumigao", slug=DEFAULT_TENANT_SLUG, status="active", plan="business"))
    db.add(User(id=WALKER_ID, email="k@x.com", password_hash="x", role="walker", tenant_id=TENANT_ID))
    db.add(User(id="adm1", email="a@x.com", password_hash="x", role="admin", tenant_id=TENANT_ID))
    db.commit()
    return db


def _earn(db, wid="w1"):
    db.add(WalkerEarning(id="we-" + wid, walker_id=WALKER_ID, tenant_id=TENANT_ID, walk_id=wid,
                         gross=30, platform_amount=5.4, amount=24.6, status=WE_ACCRUED,
                         accrued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                         payable_at=datetime(2026, 6, 10, tzinfo=timezone.utc)))
    db.commit()


def _paid_withdrawal(db):
    db.add(Payment(id="wd1", tenant_id=TENANT_ID, tutor_id=WALKER_ID, walk_id=None,
                   amount=-24.6, status="paid", provider="pix"))
    db.commit()


def test_void_after_paid_pix_emits_admin_notification(caplog):
    db = _db(); _earn(db); _paid_withdrawal(db)
    with caplog.at_level(logging.WARNING):
        out = void_walker_earning(db, "w1", reason="chargeback", source="webhook")
    db.commit()
    # Void aconteceu (não bloqueado).
    assert out is not None
    assert db.query(WalkerEarning).filter_by(walk_id="w1").one().status == WE_VOID
    # Alerta estruturado no log.
    assert any("clawback_after_pix" in r.message for r in caplog.records)
    # Notificação ao admin do tenant.
    notif = db.query(Notification).filter_by(type="walker_payout_clawback", user_id="adm1").first()
    assert notif is not None


def test_void_without_paid_pix_no_clawback_alert(caplog):
    db = _db(); _earn(db)  # sem saque pago
    with caplog.at_level(logging.WARNING):
        void_walker_earning(db, "w1", reason="chargeback", source="webhook")
    db.commit()
    assert not any("clawback_after_pix" in r.message for r in caplog.records)
    assert db.query(Notification).filter_by(type="walker_payout_clawback").count() == 0
