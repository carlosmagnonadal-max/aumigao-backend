from __future__ import annotations

import app.models  # noqa: F401

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.tenant import Tenant, TenantFeature
from app.models.pet import Pet
from app.models.pet_timeline_event import PetTimelineEvent
from app.services import pet_profile_service as svc


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="business"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    db.commit()
    return db


def test_get_or_create_config_default_off():
    db = _db()
    c = svc.get_or_create_pet_profile_config(db, "t1"); db.flush()
    assert c.profile_enabled is False
    c2 = svc.get_or_create_pet_profile_config(db, "t1")
    assert c.id == c2.id


def test_profile_active_requires_all_three_layers(monkeypatch):
    db = _db()
    tenant = db.get(Tenant, "t1")
    cfg = svc.get_or_create_pet_profile_config(db, "t1"); db.commit()
    monkeypatch.delenv("PET_LIVE_PROFILE_ENABLED", raising=False)
    assert svc.pet_profile_active(tenant, db) is False
    monkeypatch.setenv("PET_LIVE_PROFILE_ENABLED", "true")
    assert svc.pet_profile_active(tenant, db) is False
    db.add(TenantFeature(tenant_id="t1", feature_key="pet_live_profile", enabled=True)); db.commit()
    assert svc.pet_profile_active(tenant, db) is False
    cfg.profile_enabled = True; db.commit()
    assert svc.pet_profile_active(tenant, db) is True


def test_record_timeline_event():
    db = _db()
    pet = db.get(Pet, "p1")
    ev = svc.record_timeline_event(db, pet, event_type="weight", title="Peso 10kg",
                                   occurred_at=datetime(2026, 7, 1), source="tutor")
    db.commit()
    assert ev.pet_id == "p1"
    assert ev.tenant_id == "t1"
    assert ev.event_type == "weight"
    assert db.query(PetTimelineEvent).count() == 1
