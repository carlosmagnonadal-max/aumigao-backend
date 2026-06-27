# backend/tests/test_walker_earning_void.py
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.walker_earning import WalkerEarning, WE_ACCRUED, WE_VOID


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _earn(db, wid="w1", walker_id="k1"):
    db.add(WalkerEarning(id="we-"+wid, walker_id=walker_id, tenant_id="t1", walk_id=wid,
                         gross=30, platform_amount=5.4, amount=24.6, status=WE_ACCRUED,
                         accrued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                         payable_at=datetime(2026, 6, 10, tzinfo=timezone.utc)))
    db.commit()


def test_void_marks_status_and_reason():
    from app.services.walker_payout_service import void_walker_earning
    db = _db(); _earn(db)
    out = void_walker_earning(db, "w1", reason="chargeback", source="test")
    db.commit()
    assert out is not None
    e = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    assert e.status == WE_VOID and e.void_reason == "chargeback" and e.voided_at is not None


def test_void_idempotent():
    from app.services.walker_payout_service import void_walker_earning
    db = _db(); _earn(db)
    void_walker_earning(db, "w1", reason="a", source="t"); db.commit()
    first_voided_at = db.query(WalkerEarning).filter_by(walk_id="w1").one().voided_at
    out2 = void_walker_earning(db, "w1", reason="b", source="t"); db.commit()
    e = db.query(WalkerEarning).filter_by(walk_id="w1").one()
    assert e.status == WE_VOID and e.void_reason == "a"  # não sobrescreve
    assert e.voided_at == first_voided_at


def test_void_missing_earning_returns_none():
    from app.services.walker_payout_service import void_walker_earning
    db = _db()
    assert void_walker_earning(db, "nope", reason="x", source="t") is None
