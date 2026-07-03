"""Testes do serviço de passeio self-serve do tutor (Perfil Vivo 2.0, Fase D).

Cobre: serialização (contrato), título derivado, criação + evento na timeline,
evento órfão no delete, rate-limit por dia e agregação (count_in_window).
"""
from __future__ import annotations

import app.models  # noqa: F401

import json
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet import Pet
from app.models.pet_self_walk import MAX_SELF_WALKS_PER_DAY, PetSelfWalk
from app.models.pet_timeline_event import PetTimelineEvent
from app.models.tenant import Tenant
from app.models.user import User
from app.services import pet_self_walk_service as svc


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="pro"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    db.commit()
    return db


def _create(db, *, started_at=None, duration=1800, distance=1.4, needs=None, behavior=None, notes=""):
    started_at = started_at or datetime.utcnow() - timedelta(hours=1)
    pet = db.get(Pet, "p1")
    sw = svc.create_self_walk(
        db, pet, "u1",
        started_at=started_at, duration_seconds=duration, distance_km=distance,
        walk_type="rua", intensity="moderado", had_gps=True,
        needs=needs or {"pee": True, "poop": False, "water": True},
        behavior=behavior or {"interacted_people": True},
        notes=notes,
    )
    db.commit()
    db.refresh(sw)
    return sw


# ---------------------------------------------------------------------------
# Serialização
# ---------------------------------------------------------------------------

def test_self_walk_dict_shape():
    db = _db()
    sw = _create(db)
    d = svc.self_walk_dict(sw)
    assert set(d.keys()) == {
        "id", "pet_id", "started_at", "duration_seconds", "distance_km",
        "walk_type", "intensity", "had_gps", "needs", "behavior", "notes", "created_at",
    }
    assert d["needs"] == {"pee": True, "poop": False, "water": True}
    assert d["behavior"] == {
        "interacted_dogs": False, "interacted_people": True, "pulled_leash": False,
        "showed_fear": False, "showed_reactivity": False,
    }
    assert d["distance_km"] == 1.4
    assert d["had_gps"] is True


def test_self_walk_dict_distance_null():
    db = _db()
    sw = _create(db, distance=None)
    assert svc.self_walk_dict(sw)["distance_km"] is None


# ---------------------------------------------------------------------------
# Título derivado
# ---------------------------------------------------------------------------

def test_timeline_title_with_distance():
    assert svc.timeline_title(1800, 1.4) == "Passeio do tutor · 30min · 1,4km"


def test_timeline_title_without_distance():
    assert svc.timeline_title(1800, None) == "Passeio do tutor · 30min"


def test_timeline_title_hours():
    assert svc.timeline_title(3900, 3.0) == "Passeio do tutor · 1h05 · 3,0km"


# ---------------------------------------------------------------------------
# Criação + timeline
# ---------------------------------------------------------------------------

def test_create_emits_timeline_event():
    db = _db()
    sw = _create(db)
    events = db.query(PetTimelineEvent).filter(PetTimelineEvent.pet_id == "p1").all()
    assert len(events) == 1
    ev = events[0]
    assert ev.event_type == "self_walk"
    assert ev.source == "tutor"
    assert ev.related_entity_type == "pet_self_walk"
    assert ev.related_entity_id == sw.id
    payload = json.loads(ev.payload_json)
    assert payload["duration_seconds"] == 1800
    assert payload["walk_type"] == "rua"
    assert payload["needs"]["pee"] is True
    assert payload["behavior"]["interacted_people"] is True


def test_delete_keeps_timeline_event_orphan():
    """Deletar o self-walk NÃO apaga o evento (timeline = jornal append-only)."""
    db = _db()
    sw = _create(db)
    assert svc.delete_self_walk(db, "p1", sw.id) is True
    db.commit()
    assert db.query(PetSelfWalk).count() == 0
    # Evento permanece (órfão intencional).
    assert db.query(PetTimelineEvent).filter(PetTimelineEvent.event_type == "self_walk").count() == 1


def test_delete_unknown_returns_false():
    db = _db()
    assert svc.delete_self_walk(db, "p1", "nope") is False


def test_delete_scoped_to_pet():
    db = _db()
    db.add(User(id="u2", email="u2@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(Pet(id="p2", tutor_id="u2", tenant_id="t1", name="Bob"))
    db.commit()
    sw = _create(db)  # do p1
    # Tentar deletar via p2 não acha (escopo por pet_id).
    assert svc.delete_self_walk(db, "p2", sw.id) is False


# ---------------------------------------------------------------------------
# Listagem
# ---------------------------------------------------------------------------

def test_list_desc_by_started_at():
    db = _db()
    old = _create(db, started_at=datetime.utcnow() - timedelta(hours=10))
    new = _create(db, started_at=datetime.utcnow() - timedelta(hours=1))
    rows = svc.list_self_walks(db, "p1")
    assert [r.id for r in rows] == [new.id, old.id]


def test_list_respects_limit():
    db = _db()
    for i in range(3):
        _create(db, started_at=datetime.utcnow() - timedelta(hours=i + 1))
    assert len(svc.list_self_walks(db, "p1", limit=2)) == 2


# ---------------------------------------------------------------------------
# Rate-limit por dia
# ---------------------------------------------------------------------------

def test_day_limit_reached():
    db = _db()
    today = date.today()
    base = datetime(today.year, today.month, today.day, 8, 0, 0)
    for i in range(MAX_SELF_WALKS_PER_DAY):
        _create(db, started_at=base + timedelta(minutes=i * 5))
    assert svc.count_on_day(db, "p1", today) == MAX_SELF_WALKS_PER_DAY
    assert svc.day_limit_reached(db, "p1", today) is True


def test_day_limit_not_reached_below():
    db = _db()
    today = date.today()
    base = datetime(today.year, today.month, today.day, 8, 0, 0)
    _create(db, started_at=base)
    assert svc.day_limit_reached(db, "p1", today) is False


# ---------------------------------------------------------------------------
# Agregação (wellness)
# ---------------------------------------------------------------------------

def test_count_in_window():
    db = _db()
    now = datetime.utcnow()
    _create(db, started_at=now - timedelta(days=2))
    _create(db, started_at=now - timedelta(days=5))
    _create(db, started_at=now - timedelta(days=40))  # fora da janela
    n = svc.count_in_window(db, "p1", start=now - timedelta(days=30), end=now)
    assert n == 2
