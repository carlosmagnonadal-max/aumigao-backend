"""T8 — Testes do model WalkObservation + migration 0074."""
from __future__ import annotations

import app.models  # noqa: F401 — garante todos os mappers

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.walk_observation import WalkObservation, MOOD_VALUES, ENERGY_VALUES, SOCIALIZATION_VALUES


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)(), eng


def test_tablename():
    assert WalkObservation.__tablename__ == "walk_observations"


def test_walk_id_is_unique():
    """walk_id deve ter restrição unique (UniqueConstraint na tabela)."""
    db, eng = _db()
    inspector = inspect(eng)
    constraints = inspector.get_unique_constraints("walk_observations")
    # Checa que existe unique em walk_id (pode estar no UniqueConstraint ou no index)
    unique_cols = {col for c in constraints for col in c["column_names"]}
    # Também pode aparecer nos indexes unique
    indexes = inspector.get_indexes("walk_observations")
    for idx in indexes:
        if idx.get("unique"):
            unique_cols.update(idx["column_names"])
    assert "walk_id" in unique_cols, f"walk_id não é unique. unique_cols={unique_cols}, indexes={indexes}"


def test_enum_constants():
    assert "calm" in MOOD_VALUES
    assert "happy" in MOOD_VALUES
    assert "anxious" in MOOD_VALUES
    assert "agitated" in MOOD_VALUES
    assert "low" in ENERGY_VALUES
    assert "normal" in ENERGY_VALUES
    assert "high" in ENERGY_VALUES
    assert "good" in SOCIALIZATION_VALUES
    assert "neutral" in SOCIALIZATION_VALUES
    assert "reactive" in SOCIALIZATION_VALUES


def test_create_walk_observation():
    db, _ = _db()
    obs = WalkObservation(
        walk_id="w1",
        pet_id="p1",
        walker_user_id="u1",
        mood="calm",
        energy="normal",
        peed=True,
        pooped=False,
        incident=False,
        incident_notes="",
    )
    db.add(obs)
    db.commit()
    db.refresh(obs)
    assert obs.id is not None
    assert obs.incident is False
    assert obs.incident_notes == ""
    assert obs.tenant_id is None  # nullable


def test_incident_notes_default_empty():
    db, _ = _db()
    obs = WalkObservation(walk_id="w2", pet_id="p1", walker_user_id="u1")
    db.add(obs)
    db.commit()
    db.refresh(obs)
    assert obs.incident is False
    assert obs.incident_notes == ""
