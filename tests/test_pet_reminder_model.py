"""T12 — Testes do model PetReminder + migration 0075."""
from __future__ import annotations

import app.models  # noqa: F401 — garante todos os mappers

from datetime import date, datetime

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet_reminder import PetReminder, REMINDER_KINDS


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)(), eng


def test_tablename():
    assert PetReminder.__tablename__ == "pet_reminders"


def test_reminder_kinds_constant():
    assert REMINDER_KINDS == {"vaccine", "vermifuge", "birthday", "inactivity"}


def test_create_reminder_defaults():
    db, _ = _db()
    r = PetReminder(
        pet_id="p1",
        kind="vaccine",
        due_date=date(2026, 8, 1),
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    assert r.id is not None
    assert r.active is True
    assert r.last_notified_at is None
    assert r.source_event_id is None
    assert r.tenant_id is None
    assert r.created_at is not None


def test_create_reminder_all_kinds():
    db, _ = _db()
    for kind in REMINDER_KINDS:
        r = PetReminder(pet_id="p1", kind=kind, due_date=date(2026, 8, 1))
        db.add(r)
    db.commit()
    assert db.query(PetReminder).count() == 4


def test_reminder_last_notified_at_settable():
    db, _ = _db()
    now = datetime(2026, 7, 1, 12, 0)
    r = PetReminder(pet_id="p1", kind="birthday", due_date=date(2026, 7, 1), last_notified_at=now)
    db.add(r)
    db.commit()
    db.refresh(r)
    assert r.last_notified_at == now


def test_reminder_active_can_be_disabled():
    db, _ = _db()
    r = PetReminder(pet_id="p1", kind="inactivity", due_date=date(2026, 7, 1), active=False)
    db.add(r)
    db.commit()
    db.refresh(r)
    assert r.active is False
