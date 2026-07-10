"""Camada 2 do som (10/07): re-alerta de push para WalkMatchingAttempt pendente.

Hoje o walker recebe UM push (walker_attempt_created/new_walk) na criação da
tentativa; se ele não ouvir, silêncio até expirar (30min). A task
_task_walk_request_reminders reenvia o MESMO push crítico (canal walk-requests)
a cada ~5min, até 3 lembretes por tentativa, e para na hora se a tentativa sair
de pending ou o walk sair de matching (mesmo guard de process_expired_attempts).

Padrão do projeto: SQLite em memória, sem app.main, sem fixtures e2e (quebradas).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.notification import Notification
from app.models.operational_beta_log import OperationalBetaLog
from app.models.walk import Walk, WalkMatchingAttempt
from app.services import operational_scheduler_service as sched

_seq = {"n": 0}


def _next_id(prefix: str) -> str:
    _seq["n"] += 1
    return f"{prefix}-{_seq['n']}"


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.info["rls_tenant"] = "*"
    return db


def _walk(db, operational_status: str = "pending_walker_confirmation") -> Walk:
    wid = _next_id("walk")
    w = Walk(
        id=wid,
        tutor_id=_next_id("tutor"),
        walker_id=_next_id("walker"),
        pet_id=_next_id("pet"),
        scheduled_date="2024-05-10T14:00:00",
        duration_minutes=30,
        price=50.0,
        status="Agendado",
        operational_status=operational_status,
    )
    db.add(w)
    db.commit()
    return w


def _attempt(db, walk: Walk, sent_at: datetime, status: str = "pending", walker_id: str | None = None) -> WalkMatchingAttempt:
    aid = _next_id("att")
    attempt = WalkMatchingAttempt(
        id=aid,
        walk_id=walk.id,
        walker_id=walker_id or walk.walker_id,
        attempt_number=1,
        status=status,
        score=75.0,
        sent_at=sent_at,
        expires_at=sent_at + timedelta(minutes=30),
    )
    db.add(attempt)
    db.commit()
    return attempt


def _reminder_logs(db) -> list[OperationalBetaLog]:
    return (
        db.query(OperationalBetaLog)
        .filter(OperationalBetaLog.event_type == "walk_request_reminder_sent")
        .all()
    )


# ---------------------------------------------------------------------------
# Registro no ciclo do scheduler
# ---------------------------------------------------------------------------

def test_task_registered_in_cycle_after_matching_expiration():
    import inspect
    source = inspect.getsource(sched._run_operational_scheduler_cycle_locked)
    assert "walk_request_reminders" in source
    # Precisa rodar DEPOIS de matching_expiration no mesmo ciclo, para nunca
    # re-alertar uma tentativa que acabou de expirar.
    assert source.index('"matching_expiration"') < source.index('"walk_request_reminders"')


# ---------------------------------------------------------------------------
# 1º lembrete aos 5 minutos
# ---------------------------------------------------------------------------

def test_no_reminder_before_5_minutes():
    db = _db()
    walk = _walk(db)
    _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=4))

    assert sched._task_walk_request_reminders(db) == 0
    assert _reminder_logs(db) == []


def test_first_reminder_fires_at_5_minutes():
    db = _db()
    walk = _walk(db)
    attempt = _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=5, seconds=1))

    fired = sched._task_walk_request_reminders(db)
    db.commit()

    assert fired == 1
    logs = _reminder_logs(db)
    assert len(logs) == 1
    assert '"reminder_number": 1' in (logs[0].context_json or "")

    notification = db.query(Notification).filter(Notification.user_id == attempt.walker_id).first()
    assert notification is not None
    assert notification.type == "new_walk"
    assert "ainda precisa de resposta" in notification.message
    assert "10/05" in notification.message and "14:00" in notification.message


# ---------------------------------------------------------------------------
# Dedupe: não repete no mesmo ciclo / antes do próximo intervalo de 5min
# ---------------------------------------------------------------------------

def test_does_not_repeat_before_next_interval():
    db = _db()
    walk = _walk(db)
    _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=5, seconds=1))

    assert sched._task_walk_request_reminders(db) == 1
    db.commit()
    # Mesmo "ciclo" seguinte, sem o tempo avançar 5min adicionais: não reenvia.
    assert sched._task_walk_request_reminders(db) == 0
    assert len(_reminder_logs(db)) == 1


# ---------------------------------------------------------------------------
# Teto de 3 lembretes por tentativa
# ---------------------------------------------------------------------------

def test_caps_at_three_reminders_per_attempt():
    db = _db()
    walk = _walk(db)
    # sent_at bem no passado: cada chamada da task consulta o estado real do
    # banco e sempre encontra o lembrete seguinte "devido" (>= now), simulando
    # vários ciclos do scheduler em sequência.
    _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=20))

    fired_per_call = []
    for _ in range(5):
        fired_per_call.append(sched._task_walk_request_reminders(db))
        db.commit()

    assert fired_per_call == [1, 1, 1, 0, 0]
    logs = _reminder_logs(db)
    assert len(logs) == 3
    numbers = sorted(int(log.context_json.split('"reminder_number": ')[1].split(",")[0].rstrip("}")) for log in logs)
    assert numbers == [1, 2, 3]


# ---------------------------------------------------------------------------
# Para na hora: aceita / recusada / expirada / walk saiu de matching
# ---------------------------------------------------------------------------

def test_skips_accepted_attempt():
    db = _db()
    walk = _walk(db, operational_status="walker_accepted")
    _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=10), status="accepted")

    assert sched._task_walk_request_reminders(db) == 0
    assert _reminder_logs(db) == []


def test_skips_declined_attempt():
    db = _db()
    walk = _walk(db)
    _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=10), status="declined")

    assert sched._task_walk_request_reminders(db) == 0
    assert _reminder_logs(db) == []


def test_skips_expired_attempt():
    db = _db()
    walk = _walk(db)
    # expires_at no passado (attempt já expirou) — process_expired_attempts
    # ainda não rodou neste teste isolado, mas mesmo assim não deve reenviar.
    attempt = WalkMatchingAttempt(
        id=_next_id("att"),
        walk_id=walk.id,
        walker_id=walk.walker_id,
        attempt_number=1,
        status="pending",
        score=75.0,
        sent_at=datetime.utcnow() - timedelta(minutes=40),
        expires_at=datetime.utcnow() - timedelta(minutes=10),
    )
    db.add(attempt)
    db.commit()

    assert sched._task_walk_request_reminders(db) == 0
    assert _reminder_logs(db) == []


def test_skips_when_walk_left_matching():
    """Attempt ainda status=pending, mas o walk já saiu de
    pending_walker_confirmation/auto_rematching (ex.: tutor cancelou nesse meio
    tempo) — não deve re-alertar (mesmo guard de process_expired_attempts)."""
    db = _db()
    walk = _walk(db, operational_status="ride_cancelled")
    _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=10))

    assert sched._task_walk_request_reminders(db) == 0
    assert _reminder_logs(db) == []


def test_fires_for_auto_rematching_status_too():
    db = _db()
    walk = _walk(db, operational_status="auto_rematching")
    _attempt(db, walk, sent_at=datetime.utcnow() - timedelta(minutes=6))

    assert sched._task_walk_request_reminders(db) == 1
