"""Task 4 — TDD: exceção block conta como conflito de agenda no matching.

Cenários:
  1. Passeador com exceção kind='block' cobrindo o horário da solicitação
     → has_schedule_conflict retorna True (= excluído do pool / score = 0).
  2. Mesmo walker/horário SEM exceção cadastrada
     → has_schedule_conflict retorna False (zero impacto no matching atual).
"""
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.walker_availability_exception import WalkerAvailabilityException
from app.services import matching_service as ms


def _db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class _Req:
    """Stub mínimo de MatchingWalkerRequest."""

    def __init__(self, when, dur=60):
        self.scheduled_at = when
        self.duration_minutes = dur


def test_block_exception_counts_as_conflict():
    """Exceção block (dia inteiro) deve fazer has_schedule_conflict retornar True."""
    db = _db()
    db.add(
        WalkerAvailabilityException(
            id="b1",
            walker_user_id="w1",
            exception_date=date(2099, 1, 9),
            kind="block",
        )
    )
    db.commit()
    assert ms.has_schedule_conflict("w1", _Req("2099-01-09T15:00:00"), db) is True


def test_block_exception_with_time_range_covers_slot():
    """Exceção block com faixa HH:MM cobre slot dentro da janela."""
    db = _db()
    db.add(
        WalkerAvailabilityException(
            id="b2",
            walker_user_id="w2",
            exception_date=date(2099, 1, 9),
            kind="block",
            start_time="14:00",
            end_time="17:00",
        )
    )
    db.commit()
    # 15:00 está dentro de [14:00, 17:00)
    assert ms.has_schedule_conflict("w2", _Req("2099-01-09T15:00:00"), db) is True


def test_block_exception_outside_time_range_no_conflict():
    """Exceção block com faixa NÃO cobre horário fora dela — sem conflito."""
    db = _db()
    db.add(
        WalkerAvailabilityException(
            id="b3",
            walker_user_id="w3",
            exception_date=date(2099, 1, 9),
            kind="block",
            start_time="14:00",
            end_time="17:00",
        )
    )
    db.commit()
    # 18:00 está fora de [14:00, 17:00)
    assert ms.has_schedule_conflict("w3", _Req("2099-01-09T18:00:00"), db) is False


def test_open_exception_does_not_count_as_conflict():
    """Exceção kind='open' NÃO deve ser tratada como conflito (apenas abre horário)."""
    db = _db()
    db.add(
        WalkerAvailabilityException(
            id="o1",
            walker_user_id="w4",
            exception_date=date(2099, 1, 9),
            kind="open",
        )
    )
    db.commit()
    assert ms.has_schedule_conflict("w4", _Req("2099-01-09T15:00:00"), db) is False


def test_no_exception_no_extra_conflict():
    """Sem exceção cadastrada, has_schedule_conflict deve retornar False."""
    db = _db()
    assert ms.has_schedule_conflict("w1", _Req("2099-01-09T15:00:00"), db) is False
