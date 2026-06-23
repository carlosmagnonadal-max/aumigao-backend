from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.walker_availability_exception import WalkerAvailabilityException


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_exception_minimal_defaults():
    db = _db()
    exc = WalkerAvailabilityException(id="e1", walker_user_id="w1", exception_date=date(2099,1,1), kind="block")
    db.add(exc); db.commit(); db.refresh(exc)
    assert exc.kind == "block"
    assert exc.start_time is None and exc.end_time is None
    assert exc.created_at is not None and exc.updated_at is not None


def test_exception_with_time_range():
    db = _db()
    exc = WalkerAvailabilityException(id="e2", walker_user_id="w1", exception_date=date(2099,1,2), kind="open", start_time="14:00", end_time="18:00")
    db.add(exc); db.commit()
    assert exc.start_time == "14:00" and exc.end_time == "18:00"
