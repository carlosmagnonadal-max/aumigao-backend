"""Testes de unidade do weekly_mission_service.

Cobre get_or_create_weekly_missions (5 templates + idempotencia + expiracao),
update_mission_status (progress/status para cada metric_key) e expire_old_missions.

NAO importa app.main, NAO usa banco real. SQLite em memoria com apenas as tabelas
que o service toca (padrao de tests/test_recurring_plans.py).
"""
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walker_review import WalkerReview
from app.models.walker_weekly_mission import WalkerWeeklyMission
from app.services import weekly_mission_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Walk.__table__,
            WalkerReview.__table__,
            WalkerWeeklyMission.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _mission(metric_key, *, target_value, current_value, week_start=None, week_end=None,
             mission_type="m", status="not_started"):
    now = datetime.utcnow()
    return WalkerWeeklyMission(
        id=str(uuid4()),
        walker_id="w1",
        mission_type=mission_type,
        title="t",
        description="d",
        metric_key=metric_key,
        target_value=target_value,
        current_value=current_value,
        progress_percentage=0,
        status=status,
        week_start=week_start or now - timedelta(days=1),
        week_end=week_end or now + timedelta(days=5),
        reward_status="none",
    )


def _walk(walker_id, *, status, scheduled_date):
    return Walk(
        id=str(uuid4()),
        tutor_id="tutor1",
        walker_id=walker_id,
        pet_id="pet1",
        scheduled_date=scheduled_date,
        duration_minutes=30,
        price=50.0,
        status=status,
    )


# --------------------------------------------------------------------------
# get_or_create_weekly_missions
# --------------------------------------------------------------------------

def test_get_or_create_creates_five_templates():
    db = _db()
    missions = svc.get_or_create_weekly_missions("w1", db)
    assert len(missions) == 5
    types = {m.mission_type for m in missions}
    assert types == {
        "completed_walks",
        "rating",
        "active_days",
        "cancellations",
        "response_time",
    }


def test_get_or_create_sets_week_range_and_defaults():
    db = _db()
    week_start, week_end = svc.get_current_week_range()
    missions = svc.get_or_create_weekly_missions("w1", db)
    for m in missions:
        assert m.week_start == week_start
        assert m.week_end == week_end
        assert m.walker_id == "w1"
    # cancellations starts at 100% (current 0 <= target 0) -> completed
    cancel = next(m for m in missions if m.mission_type == "cancellations")
    assert cancel.progress_percentage == 100.0
    assert cancel.status == "completed"


def test_get_or_create_is_idempotent():
    db = _db()
    svc.get_or_create_weekly_missions("w1", db)
    second = svc.get_or_create_weekly_missions("w1", db)
    assert len(second) == 5
    total = db.query(WalkerWeeklyMission).filter(WalkerWeeklyMission.walker_id == "w1").count()
    assert total == 5


def test_get_or_create_completed_walks_progress_from_walks():
    db = _db()
    week_start, _ = svc.get_current_week_range()
    day = (week_start + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")
    # 5 finalizados (de target 10) -> 50%
    for _ in range(5):
        db.add(_walk("w1", status="Finalizado", scheduled_date=day))
    db.commit()

    missions = svc.get_or_create_weekly_missions("w1", db)
    completed = next(m for m in missions if m.mission_type == "completed_walks")
    assert completed.current_value == 5.0
    assert completed.progress_percentage == 50.0
    assert completed.status == "in_progress"


def test_get_or_create_completed_walks_target_reached_completes():
    db = _db()
    week_start, _ = svc.get_current_week_range()
    day = (week_start + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S")
    for _ in range(10):
        db.add(_walk("w1", status="Finalizado", scheduled_date=day))
    db.commit()

    missions = svc.get_or_create_weekly_missions("w1", db)
    completed = next(m for m in missions if m.mission_type == "completed_walks")
    assert completed.current_value == 10.0
    assert completed.progress_percentage == 100.0
    assert completed.status == "completed"
    assert completed.reward_status == "future_benefit"


def test_get_or_create_rating_mission_uses_reviews():
    db = _db()
    week_start, _ = svc.get_current_week_range()
    review_time = week_start + timedelta(hours=10)
    db.add(WalkerReview(id=str(uuid4()), walk_id="walk1", tutor_id="tutor1",
                        walker_id="w1", rating=5, created_at=review_time))
    db.add(WalkerReview(id=str(uuid4()), walk_id="walk2", tutor_id="tutor1",
                        walker_id="w1", rating=4, created_at=review_time))
    db.commit()

    missions = svc.get_or_create_weekly_missions("w1", db)
    rating = next(m for m in missions if m.mission_type == "rating")
    assert rating.current_value == 4.5
    # 4.5 / 4.7 * 100 = 95.74
    assert rating.progress_percentage == 95.74
    assert rating.status == "in_progress"


# --------------------------------------------------------------------------
# update_mission_status (sem banco, comportamento puro)
# --------------------------------------------------------------------------

def test_update_status_completed_walks_partial_in_progress():
    m = _mission("completed_walks_week", target_value=10.0, current_value=4.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 40.0
    assert m.status == "in_progress"
    assert m.completed_at is None
    assert m.expired_at is None
    assert m.reward_status == "none"


def test_update_status_not_started_when_zero():
    m = _mission("completed_walks_week", target_value=10.0, current_value=0.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 0.0
    assert m.status == "not_started"


def test_update_status_completed_caps_at_100_and_rewards():
    m = _mission("completed_walks_week", target_value=10.0, current_value=15.0)
    now = datetime.utcnow()
    svc.update_mission_status(m, now=now)
    assert m.progress_percentage == 100.0
    assert m.status == "completed"
    assert m.completed_at == now
    assert m.expired_at is None
    assert m.reward_status == "future_benefit"
    assert m.reward_description is not None


def test_update_status_expired_when_past_week_and_incomplete():
    now = datetime.utcnow()
    m = _mission(
        "completed_walks_week",
        target_value=10.0,
        current_value=2.0,
        week_start=now - timedelta(days=10),
        week_end=now - timedelta(days=3),
    )
    svc.update_mission_status(m, now=now)
    assert m.status == "expired"
    assert m.expired_at == now
    assert m.reward_status == "none"
    # progress ainda calculado (2/10 = 20%)
    assert m.progress_percentage == 20.0


def test_update_status_completed_takes_priority_even_after_week_end():
    # Mesmo expirado no tempo, se progress >= 100 a missao conta como completed.
    now = datetime.utcnow()
    m = _mission(
        "completed_walks_week",
        target_value=10.0,
        current_value=12.0,
        week_start=now - timedelta(days=10),
        week_end=now - timedelta(days=3),
    )
    svc.update_mission_status(m, now=now)
    assert m.status == "completed"
    assert m.reward_status == "future_benefit"


def test_update_status_cancellations_within_target_is_complete():
    m = _mission("cancellations_week", target_value=0.0, current_value=0.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 100.0
    assert m.status == "completed"


def test_update_status_cancellations_penalty():
    # current 2 > target 0 -> 100 - 2*25 = 50%
    m = _mission("cancellations_week", target_value=0.0, current_value=2.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 50.0
    # current_value > 0 -> in_progress
    assert m.status == "in_progress"


def test_update_status_cancellations_penalty_floor_at_zero():
    # current 5 -> 100 - 125 = -25 -> max(0, ...) = 0
    m = _mission("cancellations_week", target_value=0.0, current_value=5.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 0.0
    assert m.status == "in_progress"


def test_update_status_rating_zero_value_is_zero_progress():
    m = _mission("average_rating_week", target_value=4.7, current_value=0.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 0.0
    assert m.status == "not_started"


def test_update_status_rating_above_target_caps_at_100():
    m = _mission("average_rating_week", target_value=4.7, current_value=5.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 100.0
    assert m.status == "completed"


def test_update_status_target_zero_non_cancellation_returns_zero():
    # metric_key generico com target 0 -> calculate_progress retorna 0
    m = _mission("active_days_week", target_value=0.0, current_value=3.0)
    svc.update_mission_status(m, now=datetime.utcnow())
    assert m.progress_percentage == 0.0
    assert m.status == "in_progress"


# --------------------------------------------------------------------------
# expire_old_missions
# --------------------------------------------------------------------------

def test_expire_old_missions_marks_past_unfinished():
    db = _db()
    now = datetime.utcnow()
    m = _mission(
        "completed_walks_week",
        target_value=10.0,
        current_value=1.0,
        week_start=now - timedelta(days=10),
        week_end=now - timedelta(days=3),
        status="in_progress",
    )
    db.add(m)
    db.commit()

    svc.expire_old_missions("w1", db)
    db.refresh(m)
    assert m.status == "expired"
    assert m.expired_at is not None
    assert m.reward_status == "none"


def test_expire_old_missions_skips_completed_and_already_expired():
    db = _db()
    now = datetime.utcnow()
    done = _mission("completed_walks_week", target_value=10.0, current_value=10.0,
                    week_start=now - timedelta(days=10), week_end=now - timedelta(days=3),
                    status="completed", mission_type="a")
    already = _mission("active_days_week", target_value=4.0, current_value=1.0,
                       week_start=now - timedelta(days=10), week_end=now - timedelta(days=3),
                       status="expired", mission_type="b")
    db.add_all([done, already])
    db.commit()

    svc.expire_old_missions("w1", db)
    db.refresh(done)
    db.refresh(already)
    assert done.status == "completed"
    assert already.status == "expired"


def test_expire_old_missions_skips_current_week():
    db = _db()
    now = datetime.utcnow()
    current = _mission("completed_walks_week", target_value=10.0, current_value=1.0,
                       week_start=now - timedelta(days=1), week_end=now + timedelta(days=5),
                       status="in_progress")
    db.add(current)
    db.commit()

    svc.expire_old_missions("w1", db)
    db.refresh(current)
    assert current.status == "in_progress"


def test_expire_old_missions_only_targets_given_walker():
    db = _db()
    now = datetime.utcnow()
    other = _mission("completed_walks_week", target_value=10.0, current_value=1.0,
                     week_start=now - timedelta(days=10), week_end=now - timedelta(days=3),
                     status="in_progress")
    other.walker_id = "w2"
    db.add(other)
    db.commit()

    svc.expire_old_missions("w1", db)
    db.refresh(other)
    assert other.status == "in_progress"
