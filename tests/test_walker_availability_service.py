"""Testes TDD para walker_availability_service.is_walker_available_at.

Cobre: bloco dia inteiro, bloco por faixa, open extra, precedência block>open,
e disponibilidade recorrente real (schedule_json com chaves Seg..Dom).
"""
import json
from datetime import date, datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.database import Base
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.models.walker_availability import WalkerAvailability
from app.services import walker_availability_service as svc


def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _block(db, wid, d, s=None, e=None):
    db.add(WalkerAvailabilityException(
        id=f"b-{d}-{s}",
        walker_user_id=wid,
        exception_date=d,
        kind="block",
        start_time=s,
        end_time=e,
    ))
    db.commit()


def _open(db, wid, d, s, e):
    db.add(WalkerAvailabilityException(
        id=f"o-{d}-{s}",
        walker_user_id=wid,
        exception_date=d,
        kind="open",
        start_time=s,
        end_time=e,
    ))
    db.commit()


def _recurring(db, wid, schedule: dict):
    """Insere (ou substitui) uma linha de disponibilidade recorrente para wid."""
    row = db.query(WalkerAvailability).filter(
        WalkerAvailability.walker_user_id == wid
    ).first()
    if row:
        row.schedule_json = json.dumps(schedule)
    else:
        db.add(WalkerAvailability(walker_user_id=wid, schedule_json=json.dumps(schedule)))
    db.commit()


# ---------------------------------------------------------------------------
# Testes originais da spec (Steps 1-4)
# ---------------------------------------------------------------------------

def test_block_full_day_makes_unavailable():
    db = _db()
    _block(db, "w1", date(2099, 1, 5))
    assert svc.is_walker_available_at(db, "w1", datetime(2099, 1, 5, 15, 0)) is False


def test_block_range_only_blocks_inside():
    db = _db()
    _block(db, "w1", date(2099, 1, 6), "14:00", "16:00")
    assert svc.is_walker_available_at(db, "w1", datetime(2099, 1, 6, 15, 0)) is False
    assert svc.is_walker_available_at(db, "w1", datetime(2099, 1, 6, 10, 0)) is False


def test_open_extra_makes_available_even_without_recurring():
    db = _db()
    _open(db, "w1", date(2099, 1, 7), "08:00", "12:00")
    assert svc.is_walker_available_at(db, "w1", datetime(2099, 1, 7, 9, 0)) is True
    assert svc.is_walker_available_at(db, "w1", datetime(2099, 1, 7, 13, 0)) is False


def test_block_precedence_over_open_same_slot():
    db = _db()
    _open(db, "w1", date(2099, 1, 8), "08:00", "12:00")
    _block(db, "w1", date(2099, 1, 8), "09:00", "10:00")
    assert svc.is_walker_available_at(db, "w1", datetime(2099, 1, 8, 9, 30)) is False
    assert svc.is_walker_available_at(db, "w1", datetime(2099, 1, 8, 11, 0)) is True


# ---------------------------------------------------------------------------
# Step 5 — testes do recorrente real (schedule_json)
# ---------------------------------------------------------------------------

def test_recurring_enabled_day_slot_is_available():
    """Walker com terça habilitada e slot 09:00 → disponível às 09:xx de uma terça.

    2099-01-06 é terça-feira (weekday=1 → chave "Ter" no schedule_json).
    """
    db = _db()
    _recurring(db, "w2", {"Ter": {"enabled": True, "slots": ["09:00", "15:00"]}})
    assert svc.is_walker_available_at(db, "w2", datetime(2099, 1, 6, 9, 0)) is True


def test_recurring_slot_not_listed_is_unavailable():
    """Terça habilitada mas 10:00 não está nos slots → indisponível."""
    db = _db()
    _recurring(db, "w3", {"Ter": {"enabled": True, "slots": ["09:00", "15:00"]}})
    assert svc.is_walker_available_at(db, "w3", datetime(2099, 1, 6, 10, 0)) is False


def test_recurring_day_disabled_is_unavailable():
    """Terça com enabled=False → indisponível mesmo que slots estejam preenchidos."""
    db = _db()
    _recurring(db, "w4", {"Ter": {"enabled": False, "slots": ["09:00", "15:00"]}})
    assert svc.is_walker_available_at(db, "w4", datetime(2099, 1, 6, 9, 0)) is False


def test_recurring_no_row_is_unavailable():
    """Sem linha de disponibilidade recorrente → conservador → indisponível."""
    db = _db()
    assert svc.is_walker_available_at(db, "w5", datetime(2099, 1, 6, 9, 0)) is False


def test_block_overrides_recurring():
    """Block (exceção) precede recorrente habilitado. Usa terça 2099-01-06."""
    db = _db()
    _recurring(db, "w6", {"Ter": {"enabled": True, "slots": ["09:00"]}})
    _block(db, "w6", date(2099, 1, 6), "09:00", "10:00")
    assert svc.is_walker_available_at(db, "w6", datetime(2099, 1, 6, 9, 0)) is False


def test_open_adds_slot_outside_recurring():
    """Open (exceção) adiciona disponibilidade fora dos slots recorrentes.

    Usa terça 2099-01-06. Sem open às 11:00 → indisponível.
    """
    db = _db()
    _recurring(db, "w7", {"Ter": {"enabled": True, "slots": ["09:00"]}})
    _open(db, "w7", date(2099, 1, 6), "12:00", "13:00")
    # Slot recorrente
    assert svc.is_walker_available_at(db, "w7", datetime(2099, 1, 6, 9, 0)) is True
    # Slot extra via open
    assert svc.is_walker_available_at(db, "w7", datetime(2099, 1, 6, 12, 30)) is True
    # Fora de tudo
    assert svc.is_walker_available_at(db, "w7", datetime(2099, 1, 6, 11, 0)) is False
