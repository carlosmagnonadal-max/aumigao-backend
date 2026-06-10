import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tenant import Tenant, TenantFeature
from app.models.walker_profile import WalkerProfile
from app.services import verified_walker_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[Tenant.__table__, TenantFeature.__table__, WalkerProfile.__table__])
    return sessionmaker(bind=engine)()


def _walker(db, user_id="w1") -> WalkerProfile:
    p = WalkerProfile(id=f"p-{user_id}", user_id=user_id, full_name="Passeador", status="active")
    db.add(p); db.commit()
    return p


def test_enabled_reflects_flag():
    db = _db()
    t = Tenant(id="t1", name="A", slug="aumigao", status="active", plan="business")
    db.add(t); db.commit()
    assert svc.verified_walkers_enabled(t, db) is False
    db.add(TenantFeature(tenant_id="t1", feature_key="verified_walkers", enabled=True)); db.commit()
    assert svc.verified_walkers_enabled(t, db) is True


def test_set_verified_and_unverify():
    db = _db(); _walker(db, "w1")
    p = svc.set_verified(db, "w1", True, admin_id="admin1")
    assert p.verified is True and p.verified_at is not None and p.verified_by_admin_id == "admin1"
    p = svc.set_verified(db, "w1", False, admin_id="admin1")
    assert p.verified is False and p.verified_at is None and p.verified_by_admin_id is None


def test_set_verified_walker_not_found():
    db = _db()
    with pytest.raises(HTTPException) as e:
        svc.set_verified(db, "ghost", True, admin_id="admin1")
    assert e.value.status_code == 404
