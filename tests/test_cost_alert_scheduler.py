"""Task cost_alerts registrada no ciclo do scheduler + guard de 15 min."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.services import operational_scheduler_service as sched


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.info["rls_tenant"] = "*"
    return db


def test_task_registered_in_cycle():
    import inspect
    source = inspect.getsource(sched._run_operational_scheduler_cycle_locked)
    assert "cost_alerts" in source


def test_task_runs_and_respects_guard():
    db = _db()
    # 1ª execução avalia (0 alertas → 0) e registra o log-guard
    assert sched._task_cost_alerts(db) == 0
    db.commit()
    # 2ª execução dentro da janela de 15 min → guard pula (retorna 0 sem avaliar)
    assert sched._task_cost_alerts(db) == 0
