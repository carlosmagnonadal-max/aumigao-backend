import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.pet_tour import DEFAULT_PET_TOUR_MIN_DURATION, TenantPetTourConfig
from app.models.tenant import Tenant, TenantFeature
from app.services import pet_tour_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Tenant.__table__, TenantFeature.__table__, TenantPetTourConfig.__table__],
    )
    return sessionmaker(bind=engine)()


def _tenant(db, *, with_feature: bool) -> Tenant:
    tenant = Tenant(id="t1", name="Aumigao", slug="aumigao", status="active", plan="business")
    db.add(tenant)
    if with_feature:
        db.add(TenantFeature(tenant_id=tenant.id, feature_key="pet_tour", enabled=True))
    db.commit()
    return tenant


def test_disabled_when_no_feature():
    db = _db()
    tenant = _tenant(db, with_feature=False)
    assert svc.pet_tour_enabled(tenant, db) is False


def test_get_or_create_config_defaults():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    config = svc.get_or_create_config(db, tenant.id)
    assert config.base_price > 0
    assert config.min_duration_minutes == DEFAULT_PET_TOUR_MIN_DURATION
    assert config.active is True


def test_validate_booking_blocked_without_feature():
    db = _db()
    tenant = _tenant(db, with_feature=False)
    with pytest.raises(HTTPException) as exc:
        svc.validate_booking(db, tenant, destination="Parque", duration_minutes=120)
    assert exc.value.status_code == 403


def test_validate_booking_requires_destination():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    with pytest.raises(HTTPException) as exc:
        svc.validate_booking(db, tenant, destination="  ", duration_minutes=120)
    assert exc.value.status_code == 400


def test_validate_booking_enforces_min_duration():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    with pytest.raises(HTTPException) as exc:
        svc.validate_booking(db, tenant, destination="Parque", duration_minutes=45)
    assert exc.value.status_code == 400


def test_validate_booking_inactive_config():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    config = svc.get_or_create_config(db, tenant.id)
    config.active = False
    db.commit()
    with pytest.raises(HTTPException) as exc:
        svc.validate_booking(db, tenant, destination="Parque", duration_minutes=120)
    assert exc.value.status_code == 409


def test_validate_booking_success_returns_tenant_price():
    db = _db()
    tenant = _tenant(db, with_feature=True)
    config = svc.get_or_create_config(db, tenant.id)
    config.base_price = 199.0
    db.commit()
    result = svc.validate_booking(db, tenant, destination="Parque da Cidade", duration_minutes=120)
    assert result.base_price == 199.0
