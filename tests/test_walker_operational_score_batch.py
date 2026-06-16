"""B-ALT-006 follow-up — score operacional do passeador em BATCH (sem N+1).

calculate_walker_operational_scores(ids, db) computa o MESMO resultado que o
calculate_walker_operational_score(id, db) por passeador, mas com um numero FIXO de
queries (nao escala com a quantidade de passeadores). Estes testes travam a equivalencia
e a ausencia de N+1 no nivel do servico.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.walk_review import WalkReview
from app.services.walker_operational_score_service import (
    calculate_walker_operational_score,
    calculate_walker_operational_scores,
)


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)(), engine


def _seed_walker(db, wid, *, completed=0, ratings=(), events=(), rejected=0):
    for i in range(completed):
        db.add(Walk(id=f"{wid}-w{i}", tutor_id="t", walker_id=wid, pet_id="p",
                    operational_status="ride_completed", status="Finalizado", scheduled_date="2026-01-01",
                    duration_minutes=30, price=20))
    for i, rating in enumerate(ratings):
        db.add(WalkReview(id=f"{wid}-r{i}", walk_id=f"{wid}-rev{i}", tutor_id="t", walker_id=wid, rating=rating))
    now = datetime.utcnow()
    for i, et in enumerate(events):
        db.add(WalkOperationalEvent(id=f"{wid}-e{i}", walk_id=f"{wid}-ev{i}", walker_id=wid,
                                    event_type=et, severity="info", created_at=now - timedelta(days=1)))
    for i in range(rejected):
        db.add(WalkCompletionReview(id=f"{wid}-cr{i}", walk_id=f"{wid}-crw{i}", walker_user_id=wid,
                                    tutor_user_id="t", status="rejected"))
    db.commit()


def test_batch_matches_single_for_each_walker():
    db, _ = _db()
    _seed_walker(db, "w-a", completed=5, ratings=(5, 4, 5), events=("walker_late",), rejected=1)
    _seed_walker(db, "w-b", completed=0)  # sem dados -> baseline
    _seed_walker(db, "w-c", completed=12, ratings=(5, 5, 5, 5))

    ids = ["w-a", "w-b", "w-c"]
    batch = calculate_walker_operational_scores(ids, db)

    for wid in ids:
        single = calculate_walker_operational_score(wid, db)
        assert batch[wid] == single, f"divergencia em {wid}"


def test_batch_query_count_does_not_scale_with_walkers():
    db_small, eng_small = _db()
    for i in range(3):
        _seed_walker(db_small, f"s{i}", completed=2, ratings=(5,))
    db_big, eng_big = _db()
    for i in range(30):
        _seed_walker(db_big, f"b{i}", completed=2, ratings=(5,))

    def _count(engine, db, ids):
        n = 0
        def _on_exec(*args, **kwargs):
            nonlocal n
            n += 1
        event.listen(engine, "before_cursor_execute", _on_exec)
        try:
            calculate_walker_operational_scores(ids, db)
        finally:
            event.remove(engine, "before_cursor_execute", _on_exec)
        return n

    small = _count(eng_small, db_small, [f"s{i}" for i in range(3)])
    big = _count(eng_big, db_big, [f"b{i}" for i in range(30)])
    assert big <= small + 2, f"N+1 no batch: {small} queries p/ 3, {big} p/ 30 (deveria ser ~constante)"
