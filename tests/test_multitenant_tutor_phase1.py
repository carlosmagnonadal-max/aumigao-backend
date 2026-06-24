from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.exc import IntegrityError
import pytest

from app.core.database import Base
from app.models.tenant_tutor_access import TenantTutorAccess


def test_multi_tenant_tutor_flag(monkeypatch):
    from app.core import feature_flags
    monkeypatch.delenv("MULTI_TENANT_TUTOR", raising=False)
    assert feature_flags.multi_tenant_tutor_enabled() is False
    monkeypatch.setenv("MULTI_TENANT_TUTOR", "true")
    assert feature_flags.multi_tenant_tutor_enabled() is True


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_tenant_tutor_access_defaults():
    db = _db()
    row = TenantTutorAccess(tenant_id="t1", tutor_user_id="u1")
    db.add(row); db.commit(); db.refresh(row)
    assert row.id
    assert row.status == "active"
    assert row.initiated_by == "tutor"
    assert row.created_at is not None


def test_tenant_tutor_access_unique_constraint():
    db = _db()
    db.add(TenantTutorAccess(tenant_id="t1", tutor_user_id="u1")); db.commit()
    db.add(TenantTutorAccess(tenant_id="t1", tutor_user_id="u1"))
    with pytest.raises(IntegrityError):
        db.commit()
