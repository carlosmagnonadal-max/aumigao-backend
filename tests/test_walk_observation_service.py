"""T9 — Testes do serviço record_walk_observation (idempotência + timeline)."""
from __future__ import annotations

import app.models  # noqa: F401 — garante todos os mappers

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet import Pet
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.walk import Walk
from app.models.walk_observation import WalkObservation
from app.services import pet_profile_service as svc


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    db.add(Walk(
        id="w1", tutor_id="u1", pet_id="p1", tenant_id="t1",
        walker_id="walker1", scheduled_date="2026-07-01", duration_minutes=30, price=50.0,
    ))
    db.commit()
    return db


def test_record_creates_observation_and_timeline():
    """(a) Cria 1 WalkObservation + 1 PetTimelineEvent na primeira chamada."""
    db = _db()
    walk = db.get(Walk, "w1")
    payload = {
        "walker_user_id": "walker1",
        "mood": "calm",
        "energy": "normal",
        "peed": True,
        "pooped": False,
        "incident": False,
        "incident_notes": "",
    }
    obs = svc.record_walk_observation(db, walk, payload)
    db.commit()

    assert isinstance(obs, WalkObservation)
    assert obs.walk_id == "w1"
    assert obs.pet_id == "p1"
    assert obs.tenant_id == "t1"
    assert obs.walker_user_id == "walker1"
    assert obs.mood == "calm"

    assert db.query(WalkObservation).count() == 1
    assert db.query(PetTimelineEvent).count() == 1

    ev = db.query(PetTimelineEvent).first()
    assert ev.event_type == "walk_observation"
    assert ev.source == "walker"
    assert ev.related_entity_type == "walk"
    assert ev.related_entity_id == "w1"


def test_record_idempotent_no_duplicates():
    """(b) Chamar 2x com mesmo walk → 1 WalkObservation e 1 timeline event."""
    db = _db()
    walk = db.get(Walk, "w1")
    payload = {"walker_user_id": "walker1", "mood": "happy", "incident": False}

    obs1 = svc.record_walk_observation(db, walk, payload)
    db.commit()
    obs_id = obs1.id

    payload2 = {"walker_user_id": "walker1", "mood": "calm", "energy": "high", "incident": False}
    obs2 = svc.record_walk_observation(db, walk, payload2)
    db.commit()

    assert db.query(WalkObservation).count() == 1
    assert db.query(PetTimelineEvent).count() == 1
    # Atualiza campos
    assert obs2.mood == "calm"
    assert obs2.energy == "high"
    assert obs2.id == obs_id


def test_incident_false_zeroes_incident_notes():
    """(c) incident=False força incident_notes=""."""
    db = _db()
    walk = db.get(Walk, "w1")
    payload = {
        "walker_user_id": "walker1",
        "incident": False,
        "incident_notes": "alguma coisa que não deveria ficar",
    }
    obs = svc.record_walk_observation(db, walk, payload)
    db.commit()
    assert obs.incident_notes == ""


def test_incident_true_keeps_notes():
    """incident=True mantém incident_notes."""
    db = _db()
    walk = db.get(Walk, "w1")
    payload = {
        "walker_user_id": "walker1",
        "incident": True,
        "incident_notes": "O pet latiu muito",
    }
    obs = svc.record_walk_observation(db, walk, payload)
    db.commit()
    assert obs.incident_notes == "O pet latiu muito"
