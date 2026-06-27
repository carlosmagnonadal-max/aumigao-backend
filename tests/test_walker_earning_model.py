# backend/tests/test_walker_earning_model.py
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models  # noqa: F401
from app.core.database import Base
from app.models.walker_earning import WalkerEarning, WE_ACCRUED
from app.services.walker_earning_service import compute_payable_at

def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def test_persists():
    db = _db()
    db.add(WalkerEarning(id="we1", walker_id="k1", tenant_id="t1", walk_id="w1",
                         gross=30.0, platform_amount=5.4, amount=24.6,
                         status=WE_ACCRUED,
                         accrued_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                         payable_at=datetime(2026, 6, 10, tzinfo=timezone.utc)))
    db.commit()
    assert db.query(WalkerEarning).filter_by(walk_id="w1").one().amount == 24.6

def test_walk_id_unique():
    import pytest
    from sqlalchemy.exc import IntegrityError
    db = _db()
    for i in (1, 2):
        db.add(WalkerEarning(id=f"a{i}", walker_id="k1", tenant_id="t1", walk_id="dup",
                             gross=10, platform_amount=1, amount=9, status=WE_ACCRUED,
                             accrued_at=datetime(2026,6,1,tzinfo=timezone.utc),
                             payable_at=datetime(2026,6,10,tzinfo=timezone.utc)))
    with pytest.raises(IntegrityError):
        db.commit()

def test_payable_at_is_wednesday_of_next_week():
    # quarta 2026-06-10 (qualquer dia da semana de 08..14 jun -> quarta da semana seguinte = 17 jun)
    # semana de seg 2026-06-08 a dom 2026-06-14; quarta da semana seguinte = 2026-06-17
    got = compute_payable_at(datetime(2026, 6, 10, 14, 30, tzinfo=timezone.utc))
    assert got.year == 2026 and got.month == 6 and got.day == 17
    assert got.weekday() == 2  # quarta
    # domingo 2026-06-14 ainda é da mesma semana -> mesma quarta seguinte 2026-06-17
    got2 = compute_payable_at(datetime(2026, 6, 14, 23, 59, tzinfo=timezone.utc))
    assert got2.day == 17
    # segunda 2026-06-15 já é semana nova -> quarta seguinte = 2026-06-24
    got3 = compute_payable_at(datetime(2026, 6, 15, 0, 0, tzinfo=timezone.utc))
    assert got3.day == 24
