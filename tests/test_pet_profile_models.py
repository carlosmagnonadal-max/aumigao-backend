from __future__ import annotations

import app.models  # noqa: F401

from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet import Pet
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.pet_profile_config import PetProfileConfig


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_pet_new_longitudinal_fields():
    db = _db()
    p = Pet(id="p1", tutor_id="u1", name="Rex", birth_date=date(2020, 1, 1),
            chip_number="123", vet_name="Dr Vet", vet_phone="9999", emergency_contact="8888")
    db.add(p); db.commit(); db.refresh(p)
    assert p.birth_date == date(2020, 1, 1)
    assert p.chip_number == "123"
    assert p.vet_name == "Dr Vet"


def test_timeline_event_defaults():
    db = _db()
    e = PetTimelineEvent(id="e1", pet_id="p1", event_type="weight", title="Peso",
                         occurred_at=datetime(2026, 7, 1))
    db.add(e); db.commit(); db.refresh(e)
    assert e.source == "tutor"
    assert e.notes == ""


def test_profile_config_defaults():
    db = _db()
    c = PetProfileConfig(tenant_id="t1")
    db.add(c); db.commit(); db.refresh(c)
    assert c.profile_enabled is False
    assert c.observations_enabled is False
    assert c.reminders_enabled is False
    assert c.vaccine_lead_days == 15
    assert c.inactivity_days == 10
