"""Testes de unidade para operational_reliability_service.

Cobre: WALKER_LATE, MISSING_CHECKIN, LATE_CANCELLATION (janelas de tempo),
dedupe de eventos, criacao/serializacao e bordas.

Padrao: SQLite em memoria (sem app.main, sem banco real, sem alembic).
"""
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walk_operational_event import WalkOperationalEvent
from app.services import operational_reliability_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Walk.__table__,
            WalkOperationalEvent.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _walk(db, *, scheduled_offset_min=0, status="ride_scheduled",
          operational_status=None, walker_id="walker1", tutor_id="tutor1",
          assigned_walker_id=None, scheduled_date=None):
    """Cria um Walk. scheduled_offset_min: minutos a partir de utcnow (negativo = passado)."""
    if scheduled_date is None:
        scheduled_at = datetime.utcnow() + timedelta(minutes=scheduled_offset_min)
        scheduled_date = scheduled_at.replace(microsecond=0).isoformat()
    walk = Walk(
        id=str(uuid4()),
        tutor_id=tutor_id,
        walker_id=walker_id,
        assigned_walker_id=assigned_walker_id,
        pet_id="pet1",
        scheduled_date=scheduled_date,
        duration_minutes=30,
        price=50.0,
        status=status,
        operational_status=operational_status if operational_status is not None else status,
    )
    db.add(walk)
    db.commit()
    db.refresh(walk)
    return walk


def _count(db, walk_id, event_type):
    return (
        db.query(WalkOperationalEvent)
        .filter(WalkOperationalEvent.walk_id == walk_id,
                WalkOperationalEvent.event_type == event_type)
        .count()
    )


# --------------------------------------------------------------------------- #
# _parse_scheduled_at
# --------------------------------------------------------------------------- #
def test_parse_scheduled_at_handles_z_suffix_and_strips_tz():
    parsed = svc._parse_scheduled_at("2026-06-09T10:00:00Z")
    assert parsed == datetime(2026, 6, 9, 10, 0, 0)
    assert parsed.tzinfo is None


def test_parse_scheduled_at_invalid_and_empty_returns_none():
    assert svc._parse_scheduled_at(None) is None
    assert svc._parse_scheduled_at("   ") is None
    assert svc._parse_scheduled_at("nao-e-data") is None


# --------------------------------------------------------------------------- #
# _walk_status_key — operational_status tem prioridade sobre status
# --------------------------------------------------------------------------- #
def test_walk_status_key_prefers_operational_status():
    db = _db()
    walk = _walk(db, status="Agendado", operational_status="walker_arriving")
    assert svc._walk_status_key(walk) == "walker_arriving"


def test_walk_status_key_falls_back_to_status_when_operational_blank():
    db = _db()
    walk = _walk(db, status="Agendado", operational_status="")
    assert svc._walk_status_key(walk) == "Agendado"


# --------------------------------------------------------------------------- #
# create_operational_event + dedupe
# --------------------------------------------------------------------------- #
def test_create_operational_event_basic_fields():
    db = _db()
    walk = _walk(db, walker_id="walkerX", tutor_id="tutorX")
    event = svc.create_operational_event(db, walk, svc.WALKER_LATE, "medium", "nota")
    db.commit()
    assert event is not None
    assert event.walk_id == walk.id
    assert event.walker_id == "walkerX"
    assert event.tutor_id == "tutorX"
    assert event.event_type == svc.WALKER_LATE
    assert event.severity == "medium"
    assert event.notes == "nota"


def test_create_operational_event_uses_assigned_walker_when_walker_id_missing():
    db = _db()
    walk = _walk(db, walker_id=None, assigned_walker_id="assigned1")
    event = svc.create_operational_event(db, walk, svc.WALKER_LATE)
    db.commit()
    assert event.walker_id == "assigned1"


def test_create_operational_event_default_notes_from_labels():
    db = _db()
    walk = _walk(db)
    event = svc.create_operational_event(db, walk, svc.MISSING_CHECKIN)
    db.commit()
    assert event.notes == svc.EVENT_LABELS[svc.MISSING_CHECKIN]


def test_create_operational_event_invalid_severity_defaults_to_low():
    db = _db()
    walk = _walk(db)
    event = svc.create_operational_event(db, walk, svc.WALKER_LATE, severity="catastrofico")
    db.commit()
    assert event.severity == "low"


def test_create_operational_event_dedupe_blocks_second_same_type():
    db = _db()
    walk = _walk(db)
    first = svc.create_operational_event(db, walk, svc.WALKER_LATE)
    db.commit()
    second = svc.create_operational_event(db, walk, svc.WALKER_LATE)
    assert first is not None
    assert second is None
    assert _count(db, walk.id, svc.WALKER_LATE) == 1


def test_create_operational_event_dedupe_disabled_allows_duplicate():
    db = _db()
    walk = _walk(db)
    svc.create_operational_event(db, walk, svc.WALKER_LATE)
    db.commit()
    second = svc.create_operational_event(db, walk, svc.WALKER_LATE, dedupe=False)
    db.commit()
    assert second is not None
    assert _count(db, walk.id, svc.WALKER_LATE) == 2


def test_create_operational_event_dedupe_per_event_type():
    db = _db()
    walk = _walk(db)
    svc.create_operational_event(db, walk, svc.WALKER_LATE)
    db.commit()
    other = svc.create_operational_event(db, walk, svc.MISSING_CHECKIN)
    db.commit()
    assert other is not None
    assert _count(db, walk.id, svc.WALKER_LATE) == 1
    assert _count(db, walk.id, svc.MISSING_CHECKIN) == 1


# --------------------------------------------------------------------------- #
# detect_reliability_events — WALKER_LATE (janela de atraso)
# --------------------------------------------------------------------------- #
def test_detect_no_scheduled_date_returns_empty():
    db = _db()
    walk = _walk(db, scheduled_date="")
    assert svc.detect_reliability_events(walk, db) == []


def test_detect_walker_late_fires_after_window():
    db = _db()
    # walker_arriving e ja passou 20+ min do horario
    walk = _walk(db, scheduled_offset_min=-25, operational_status="walker_arriving",
                 status="walker_arriving")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    types = {e.event_type for e in created}
    assert svc.WALKER_LATE in types
    assert _count(db, walk.id, svc.WALKER_LATE) == 1


def test_detect_walker_late_not_fired_before_window():
    db = _db()
    # walker_arriving mas so 5 min de atraso (< 20)
    walk = _walk(db, scheduled_offset_min=-5, operational_status="walker_arriving",
                 status="walker_arriving")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    assert svc.WALKER_LATE not in {e.event_type for e in created}
    assert _count(db, walk.id, svc.WALKER_LATE) == 0


def test_detect_walker_late_requires_walker_arriving_status():
    db = _db()
    # atraso suficiente, mas status nao e walker_arriving -> sem WALKER_LATE
    walk = _walk(db, scheduled_offset_min=-30, operational_status="ride_scheduled",
                 status="ride_scheduled")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    assert svc.WALKER_LATE not in {e.event_type for e in created}


# --------------------------------------------------------------------------- #
# detect_reliability_events — MISSING_CHECKIN (janela maior)
# --------------------------------------------------------------------------- #
def test_detect_missing_checkin_fires_after_window():
    db = _db()
    # status ativo pre-inicio e 45+ min de atraso
    walk = _walk(db, scheduled_offset_min=-50, operational_status="ride_scheduled",
                 status="ride_scheduled")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    assert svc.MISSING_CHECKIN in {e.event_type for e in created}
    assert _count(db, walk.id, svc.MISSING_CHECKIN) == 1


def test_detect_missing_checkin_not_fired_before_window():
    db = _db()
    walk = _walk(db, scheduled_offset_min=-30, operational_status="ride_scheduled",
                 status="ride_scheduled")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    assert svc.MISSING_CHECKIN not in {e.event_type for e in created}


def test_detect_missing_checkin_requires_active_pre_start_status():
    db = _db()
    # atraso enorme mas status fora do conjunto ACTIVE_PRE_START_STATUSES
    walk = _walk(db, scheduled_offset_min=-120, operational_status="walk_in_progress",
                 status="walk_in_progress")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    assert svc.MISSING_CHECKIN not in {e.event_type for e in created}


def test_detect_walker_arriving_fires_both_late_and_missing_checkin():
    db = _db()
    # walker_arriving esta em ACTIVE_PRE_START_STATUSES E dispara WALKER_LATE;
    # com 50 min ambos disparam.
    walk = _walk(db, scheduled_offset_min=-50, operational_status="walker_arriving",
                 status="walker_arriving")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    types = {e.event_type for e in created}
    assert svc.WALKER_LATE in types
    assert svc.MISSING_CHECKIN in types


def test_detect_is_idempotent_due_to_dedupe():
    db = _db()
    walk = _walk(db, scheduled_offset_min=-50, operational_status="walker_arriving",
                 status="walker_arriving")
    first = svc.detect_reliability_events(walk, db)
    db.commit()
    second = svc.detect_reliability_events(walk, db)
    db.commit()
    assert len(first) == 2
    assert second == []
    assert _count(db, walk.id, svc.WALKER_LATE) == 1
    assert _count(db, walk.id, svc.MISSING_CHECKIN) == 1


def test_detect_respects_env_window_override(monkeypatch):
    db = _db()
    monkeypatch.setenv("OPERATIONAL_WALKER_LATE_MINUTES", "5")
    # 10 min de atraso, janela reduzida para 5 -> dispara
    walk = _walk(db, scheduled_offset_min=-10, operational_status="walker_arriving",
                 status="walker_arriving")
    created = svc.detect_reliability_events(walk, db)
    db.commit()
    assert svc.WALKER_LATE in {e.event_type for e in created}


def test_int_env_invalid_or_nonpositive_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "naoehnumero")
    assert svc._int_env("SOME_VAR", 20) == 20
    monkeypatch.setenv("SOME_VAR", "0")
    assert svc._int_env("SOME_VAR", 20) == 20
    monkeypatch.setenv("SOME_VAR", "-3")
    assert svc._int_env("SOME_VAR", 20) == 20
    monkeypatch.setenv("SOME_VAR", "7")
    assert svc._int_env("SOME_VAR", 20) == 7


# --------------------------------------------------------------------------- #
# record_late_cancellation_if_applicable
# --------------------------------------------------------------------------- #
def test_late_cancellation_fires_when_cancelled_near_schedule():
    db = _db()
    # cancelado e dentro da janela (horario ja passou)
    walk = _walk(db, scheduled_offset_min=-10, operational_status="ride_cancelled",
                 status="ride_cancelled")
    event = svc.record_late_cancellation_if_applicable(walk, db)
    db.commit()
    assert event is not None
    assert event.event_type == svc.LATE_CANCELLATION
    assert event.severity == "medium"


def test_late_cancellation_within_window_before_schedule():
    db = _db()
    # cancelado 30 min ANTES do horario; janela=60 -> ainda dentro (>= scheduled-60)
    walk = _walk(db, scheduled_offset_min=30, operational_status="cancelled",
                 status="cancelled")
    event = svc.record_late_cancellation_if_applicable(walk, db)
    db.commit()
    assert event is not None
    assert event.event_type == svc.LATE_CANCELLATION


def test_late_cancellation_not_fired_too_early():
    db = _db()
    # cancelado 120 min antes do horario; janela=60 -> fora da janela
    walk = _walk(db, scheduled_offset_min=120, operational_status="cancelled",
                 status="cancelled")
    event = svc.record_late_cancellation_if_applicable(walk, db)
    db.commit()
    assert event is None


def test_late_cancellation_requires_cancelled_status():
    db = _db()
    walk = _walk(db, scheduled_offset_min=-10, operational_status="ride_scheduled",
                 status="ride_scheduled")
    event = svc.record_late_cancellation_if_applicable(walk, db)
    db.commit()
    assert event is None


def test_late_cancellation_no_scheduled_date_returns_none():
    db = _db()
    walk = _walk(db, scheduled_date="", operational_status="cancelled", status="cancelled")
    assert svc.record_late_cancellation_if_applicable(walk, db) is None


def test_late_cancellation_dedupe():
    db = _db()
    walk = _walk(db, scheduled_offset_min=-10, operational_status="cancelled",
                 status="cancelled")
    first = svc.record_late_cancellation_if_applicable(walk, db)
    db.commit()
    second = svc.record_late_cancellation_if_applicable(walk, db)
    db.commit()
    assert first is not None
    assert second is None
    assert _count(db, walk.id, svc.LATE_CANCELLATION) == 1


# --------------------------------------------------------------------------- #
# record_operational_recovery
# --------------------------------------------------------------------------- #
def test_record_operational_recovery_creates_high_severity_event():
    db = _db()
    walk = _walk(db)
    event = svc.record_operational_recovery(walk, db)
    db.commit()
    assert event is not None
    assert event.event_type == svc.OPERATIONAL_RECOVERY_TRIGGERED
    assert event.severity == "high"


def test_record_operational_recovery_dedupe():
    db = _db()
    walk = _walk(db)
    svc.record_operational_recovery(walk, db)
    db.commit()
    assert svc.record_operational_recovery(walk, db) is None


# --------------------------------------------------------------------------- #
# serialize_operational_event
# --------------------------------------------------------------------------- #
def test_serialize_operational_event_maps_labels():
    db = _db()
    walk = _walk(db, walker_id="w1", tutor_id="t1")
    event = svc.create_operational_event(db, walk, svc.WALKER_LATE, "medium")
    db.commit()
    db.refresh(event)
    data = svc.serialize_operational_event(event)
    assert data["walk_id"] == walk.id
    assert data["walker_id"] == "w1"
    assert data["tutor_id"] == "t1"
    assert data["event_type"] == svc.WALKER_LATE
    assert data["label"] == svc.EVENT_LABELS[svc.WALKER_LATE]
    assert data["severity"] == "medium"
    assert data["severity_label"] == svc.SEVERITY_LABELS["medium"]
    assert data["created_at"] is not None


def test_serialize_operational_event_unknown_type_falls_back_to_raw():
    db = _db()
    walk = _walk(db)
    # cria evento com tipo desconhecido (sem label)
    event = svc.create_operational_event(db, walk, "tipo_desconhecido")
    db.commit()
    db.refresh(event)
    data = svc.serialize_operational_event(event)
    assert data["label"] == "tipo_desconhecido"
    # severity 'low' tem label conhecido
    assert data["severity_label"] == svc.SEVERITY_LABELS["low"]
