from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401  (registra todos os models no Base)
from app.core.database import Base
from app.models.commission_entry import CommissionEntry, COMM_ACCRUED

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def test_commission_entry_persists_snapshot():
    db = _db()
    e = CommissionEntry(
        id="ce-1", tenant_id="t1", walk_id="w1", period="2026-06",
        walk_price=30.0, commission_percent=10.0, amount=3.0,
        is_network=False, status=COMM_ACCRUED,
    )
    db.add(e); db.commit()
    got = db.query(CommissionEntry).filter_by(walk_id="w1").one()
    assert got.amount == 3.0
    assert got.commission_percent == 10.0
    assert got.status == COMM_ACCRUED
    assert got.is_network is False

def test_walk_id_is_unique():
    import pytest
    from sqlalchemy.exc import IntegrityError
    db = _db()
    db.add(CommissionEntry(id="a", tenant_id="t1", walk_id="dup", period="2026-06",
                           walk_price=10, commission_percent=10, amount=1, is_network=False, status=COMM_ACCRUED))
    db.commit()
    db.add(CommissionEntry(id="b", tenant_id="t1", walk_id="dup", period="2026-06",
                           walk_price=10, commission_percent=10, amount=1, is_network=False, status=COMM_ACCRUED))
    with pytest.raises(IntegrityError):
        db.commit()
