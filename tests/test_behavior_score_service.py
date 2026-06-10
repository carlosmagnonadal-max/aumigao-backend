from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walker_review import WalkerReview
from app.services.behavior_score_service import clamp, get_behavior_score


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[Walk.__table__, WalkerReview.__table__],
    )
    return sessionmaker(bind=engine)()


_walk_counter = {"n": 0}


def _walk(db, walker_id, *, status="Finalizado", created_at=None):
    _walk_counter["n"] += 1
    wid = f"w{_walk_counter['n']}"
    walk = Walk(
        id=wid,
        tutor_id="tutor1",
        pet_id="pet1",
        walker_id=walker_id,
        scheduled_date="2026-01-01",
        duration_minutes=30,
        price=50.0,
        status=status,
    )
    db.add(walk)
    db.commit()
    if created_at is not None:
        # created_at has a default; override after insert to control consistency calc
        walk.created_at = created_at
        db.commit()
    return walk


_review_counter = {"n": 0}


def _review(db, walker_id, rating, *, created_at=None):
    _review_counter["n"] += 1
    rid = f"r{_review_counter['n']}"
    review = WalkerReview(
        id=rid,
        walk_id=f"wkrev{_review_counter['n']}",
        tutor_id="tutor1",
        walker_id=walker_id,
        rating=rating,
    )
    db.add(review)
    db.commit()
    if created_at is not None:
        review.created_at = created_at
        db.commit()
    return review


# ---------------------------------------------------------------------------
# clamp helper
# ---------------------------------------------------------------------------

def test_clamp_within_bounds():
    assert clamp(50) == 50


def test_clamp_below_min_floors_to_zero():
    assert clamp(-10) == 0


def test_clamp_above_max_caps_at_100():
    assert clamp(150) == 100


def test_clamp_custom_bounds():
    assert clamp(5, 1, 3) == 3
    assert clamp(0, 1, 3) == 1


# ---------------------------------------------------------------------------
# No data at all -> every component falls back to defaults
# ---------------------------------------------------------------------------

def test_no_walks_no_reviews_returns_defaults():
    db = _db()
    result = get_behavior_score("walkerX", db)

    # No walks: acceptance=75, response=75 (no completed), consistency=75 (no active days),
    # recent_rating=75 (no reviews). cancellation: 0 cancelled / max(1,0)=1 -> 100.
    assert result["acceptance_rate_score"] == 75.0
    assert result["response_time_score"] == 75.0
    assert result["consistency_score"] == 75.0
    assert result["recent_rating_score"] == 75.0
    assert result["cancellation_score"] == 100.0

    expected = round(75 * 0.30 + 100 * 0.25 + 75 * 0.20 + 75 * 0.15 + 75 * 0.10, 2)
    assert result["behavior_score"] == expected


# ---------------------------------------------------------------------------
# Walks present toggles acceptance/response scores
# ---------------------------------------------------------------------------

def test_walks_present_raises_acceptance_score():
    db = _db()
    _walk(db, "walkerA", status="Finalizado")
    result = get_behavior_score("walkerA", db)
    assert result["acceptance_rate_score"] == 82.0


def test_walk_not_completed_keeps_response_default():
    db = _db()
    # A walk exists (acceptance becomes 82) but it's not completed -> response stays 75.
    _walk(db, "walkerB", status="Agendado")
    result = get_behavior_score("walkerB", db)
    assert result["acceptance_rate_score"] == 82.0
    assert result["response_time_score"] == 75.0


def test_completed_walk_raises_response_score():
    db = _db()
    _walk(db, "walkerC", status="completed")
    result = get_behavior_score("walkerC", db)
    assert result["response_time_score"] == 84.0


# ---------------------------------------------------------------------------
# Cancellation rate
# ---------------------------------------------------------------------------

def test_cancellation_score_with_half_cancelled():
    db = _db()
    _walk(db, "walkerD", status="Finalizado")
    _walk(db, "walkerD", status="cancelado")
    result = get_behavior_score("walkerD", db)
    # cancellation_rate = 1/2 = 0.5 -> 100 - 50 = 50
    assert result["cancellation_score"] == 50.0


def test_cancellation_matching_is_case_insensitive_and_trimmed():
    db = _db()
    _walk(db, "walkerE", status="  CanCeLaDo  ")
    result = get_behavior_score("walkerE", db)
    # 1 cancelled / 1 total = 1.0 -> 100 - 100 = 0
    assert result["cancellation_score"] == 0.0


def test_all_cancelled_floors_cancellation_to_zero():
    db = _db()
    _walk(db, "walkerF", status="cancelado")
    _walk(db, "walkerF", status="cancelado")
    result = get_behavior_score("walkerF", db)
    assert result["cancellation_score"] == 0.0


# ---------------------------------------------------------------------------
# Consistency (active days = distinct created_at dates among completed walks)
# ---------------------------------------------------------------------------

def test_consistency_counts_distinct_days_among_completed():
    db = _db()
    day1 = datetime(2026, 1, 1, 10, 0, 0)
    day1b = datetime(2026, 1, 1, 18, 0, 0)  # same date -> not a new active day
    day2 = datetime(2026, 1, 2, 9, 0, 0)
    _walk(db, "walkerG", status="Finalizado", created_at=day1)
    _walk(db, "walkerG", status="Finalizado", created_at=day1b)
    _walk(db, "walkerG", status="Finalizado", created_at=day2)
    result = get_behavior_score("walkerG", db)
    # 2 distinct days -> 2 * 8 = 16
    assert result["consistency_score"] == 16.0


def test_consistency_caps_at_100():
    db = _db()
    # 13+ distinct days -> 13*8=104 -> clamped to 100
    for i in range(1, 16):
        _walk(db, "walkerH", status="Finalizado", created_at=datetime(2026, 2, i, 12, 0, 0))
    result = get_behavior_score("walkerH", db)
    assert result["consistency_score"] == 100.0


def test_consistency_ignores_non_completed_walks():
    db = _db()
    # Only cancelled/scheduled walks -> no completed -> active_days falsy -> default 75
    _walk(db, "walkerI", status="cancelado", created_at=datetime(2026, 1, 1, 12, 0, 0))
    _walk(db, "walkerI", status="Agendado", created_at=datetime(2026, 1, 2, 12, 0, 0))
    result = get_behavior_score("walkerI", db)
    assert result["consistency_score"] == 75.0


# ---------------------------------------------------------------------------
# Recent rating (reviews within last 45 days)
# ---------------------------------------------------------------------------

def test_recent_rating_averages_recent_reviews():
    db = _db()
    recent = datetime.utcnow() - timedelta(days=5)
    _review(db, "walkerJ", 4, created_at=recent)
    _review(db, "walkerJ", 5, created_at=recent)
    result = get_behavior_score("walkerJ", db)
    # avg 4.5 / 5 * 100 = 90
    assert result["recent_rating_score"] == 90.0


def test_old_reviews_excluded_from_recent_rating():
    db = _db()
    old = datetime.utcnow() - timedelta(days=60)
    _review(db, "walkerK", 5, created_at=old)
    result = get_behavior_score("walkerK", db)
    # No review within 45 days -> default 75
    assert result["recent_rating_score"] == 75.0


def test_recent_rating_perfect_score_caps_at_100():
    db = _db()
    recent = datetime.utcnow() - timedelta(days=1)
    _review(db, "walkerL", 5, created_at=recent)
    result = get_behavior_score("walkerL", db)
    # 5/5*100 = 100
    assert result["recent_rating_score"] == 100.0


# ---------------------------------------------------------------------------
# Isolation: data of other walkers must not leak
# ---------------------------------------------------------------------------

def test_only_target_walker_data_counted():
    db = _db()
    _walk(db, "walkerM", status="cancelado")
    _walk(db, "otherWalker", status="Finalizado")
    result = get_behavior_score("walkerM", db)
    # walkerM has 1 walk (cancelled) -> rate 1.0 -> cancellation 0
    assert result["cancellation_score"] == 0.0
    assert result["acceptance_rate_score"] == 82.0


# ---------------------------------------------------------------------------
# Full integration of weighted aggregate + rounding
# ---------------------------------------------------------------------------

def test_behavior_score_weighted_aggregate():
    db = _db()
    recent = datetime.utcnow() - timedelta(days=3)
    _walk(db, "walkerN", status="Finalizado", created_at=datetime(2026, 1, 1, 12, 0, 0))
    _walk(db, "walkerN", status="cancelado")
    _review(db, "walkerN", 4, created_at=recent)
    result = get_behavior_score("walkerN", db)

    acceptance = 82.0          # walks exist
    cancellation = 50.0        # 1 cancelled / 2 total
    response = 84.0            # has completed walk
    rating = clamp(4 / 5 * 100)  # 80
    consistency = clamp(1 * 8)   # 8
    expected = round(
        acceptance * 0.30
        + cancellation * 0.25
        + response * 0.20
        + rating * 0.15
        + consistency * 0.10,
        2,
    )
    assert result["behavior_score"] == expected
    assert result["recent_rating_score"] == 80.0
    assert result["consistency_score"] == 8.0


def test_returned_dict_has_expected_keys():
    db = _db()
    result = get_behavior_score("walkerO", db)
    assert set(result.keys()) == {
        "behavior_score",
        "acceptance_rate_score",
        "cancellation_score",
        "response_time_score",
        "recent_rating_score",
        "consistency_score",
    }
