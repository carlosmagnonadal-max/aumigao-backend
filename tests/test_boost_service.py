from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walker_boost import WalkerBoost
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.services import boost_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WalkerBoost.__table__,
            WalkerProfile.__table__,
            WalkerReview.__table__,
            Walk.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _profile(db, walker_id: str, *, status: str = "approved") -> WalkerProfile:
    profile = WalkerProfile(
        id=str(uuid4()),
        user_id=walker_id,
        status=status,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _review(db, walker_id: str, rating: int, *, is_flagged: bool = False) -> WalkerReview:
    # walk_id must be unique (UniqueConstraint on walk_id)
    review = WalkerReview(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        tutor_id=str(uuid4()),
        walker_id=walker_id,
        rating=rating,
        is_flagged=is_flagged,
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    return review


def _boost(db, walker_id: str, **kwargs) -> WalkerBoost:
    defaults = dict(
        id=str(uuid4()),
        walker_id=walker_id,
        boost_enabled=True,
        boost_score=3,
        boost_status="active",
        boost_start_at=None,
        boost_end_at=None,
        updated_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    boost = WalkerBoost(**defaults)
    db.add(boost)
    db.commit()
    db.refresh(boost)
    return boost


# --------------------------------------------------------------------------
# validate_boost_eligibility
# --------------------------------------------------------------------------

def test_eligibility_no_profile():
    db = _db()
    ok, msg = svc.validate_boost_eligibility(None, "w1", db)
    assert ok is False
    assert msg == "Perfil nao encontrado"


def test_eligibility_status_not_approved():
    db = _db()
    profile = _profile(db, "w1", status="pending")
    ok, msg = svc.validate_boost_eligibility(profile, "w1", db)
    assert ok is False
    assert msg == "Boost apenas para passeador approved"


def test_eligibility_risk_suspended_blocks():
    # profile.status in {suspended, blocked} -> determine_risk_level == "suspended"
    db = _db()
    profile = _profile(db, "w1", status="suspended")
    ok, msg = svc.validate_boost_eligibility(profile, "w1", db)
    # blocked earlier by status != approved (status check runs first)
    assert ok is False
    assert msg == "Boost apenas para passeador approved"


def test_eligibility_risk_critical_blocks():
    # approved profile, but 3+ flagged reviews -> risk_level "critical"
    db = _db()
    profile = _profile(db, "w1", status="approved")
    for _ in range(3):
        _review(db, "w1", rating=5, is_flagged=True)
    ok, msg = svc.validate_boost_eligibility(profile, "w1", db)
    assert ok is False
    assert msg == "Boost bloqueado para revisao de qualidade"


def test_eligibility_rating_below_minimum_blocks():
    # approved, not critical risk, but rating average < 4.5 with reviews -> blocked
    db = _db()
    profile = _profile(db, "w1", status="approved")
    # 2 reviews so reviews_count < 3 (avoids critical/risk thresholds), avg 4.0 < 4.5
    _review(db, "w1", rating=4)
    _review(db, "w1", rating=4)
    ok, msg = svc.validate_boost_eligibility(profile, "w1", db)
    assert ok is False
    assert msg == "Avaliacao minima para boost nao atingida"


def test_eligibility_happy_path_no_reviews():
    # approved, no reviews -> reviews_count == 0 -> rating gate skipped -> eligible
    db = _db()
    profile = _profile(db, "w1", status="approved")
    ok, msg = svc.validate_boost_eligibility(profile, "w1", db)
    assert ok is True
    assert msg == "Elegivel para boost controlado"


def test_eligibility_happy_path_high_rating():
    # approved, 2 reviews avg 5.0 >= 4.5 -> eligible
    db = _db()
    profile = _profile(db, "w1", status="approved")
    _review(db, "w1", rating=5)
    _review(db, "w1", rating=5)
    ok, msg = svc.validate_boost_eligibility(profile, "w1", db)
    assert ok is True
    assert msg == "Elegivel para boost controlado"


def test_eligibility_rating_exactly_4_5_is_allowed():
    # boundary: rating_average exactly 4.5 -> NOT < 4.5 -> eligible
    db = _db()
    profile = _profile(db, "w1", status="approved")
    _review(db, "w1", rating=4)
    _review(db, "w1", rating=5)  # avg 4.5
    ok, _ = svc.validate_boost_eligibility(profile, "w1", db)
    assert ok is True


# --------------------------------------------------------------------------
# boost_score_for_walker (clamp 0-5)
# --------------------------------------------------------------------------

def test_score_zero_when_not_eligible():
    db = _db()
    profile = _profile(db, "w1", status="pending")
    _boost(db, "w1", boost_score=4)
    assert svc.boost_score_for_walker(profile, "w1", db) == 0.0


def test_score_zero_when_eligible_but_no_active_boost():
    db = _db()
    profile = _profile(db, "w1", status="approved")
    # no boost row at all
    assert svc.boost_score_for_walker(profile, "w1", db) == 0.0


def test_score_returns_active_boost_score():
    db = _db()
    profile = _profile(db, "w1", status="approved")
    _boost(db, "w1", boost_score=3)
    assert svc.boost_score_for_walker(profile, "w1", db) == 3.0


def test_score_clamped_to_max_5():
    db = _db()
    profile = _profile(db, "w1", status="approved")
    _boost(db, "w1", boost_score=9)
    assert svc.boost_score_for_walker(profile, "w1", db) == 5.0


def test_score_clamped_to_min_0_for_negative():
    db = _db()
    profile = _profile(db, "w1", status="approved")
    _boost(db, "w1", boost_score=-3)
    assert svc.boost_score_for_walker(profile, "w1", db) == 0.0


def test_score_none_treated_as_zero():
    db = _db()
    profile = _profile(db, "w1", status="approved")
    _boost(db, "w1", boost_score=None)
    assert svc.boost_score_for_walker(profile, "w1", db) == 0.0


# --------------------------------------------------------------------------
# active_boost_for_walker (expiration / window / status)
# --------------------------------------------------------------------------

def test_active_boost_none_when_no_boost():
    db = _db()
    assert svc.active_boost_for_walker("w1", db) is None


def test_active_boost_none_when_disabled():
    db = _db()
    _boost(db, "w1", boost_enabled=False)
    assert svc.active_boost_for_walker("w1", db) is None


def test_active_boost_none_when_status_not_active():
    db = _db()
    _boost(db, "w1", boost_enabled=True, boost_status="inactive")
    assert svc.active_boost_for_walker("w1", db) is None


def test_active_boost_none_when_not_started_yet():
    db = _db()
    _boost(db, "w1", boost_start_at=datetime.utcnow() + timedelta(days=1))
    assert svc.active_boost_for_walker("w1", db) is None


def test_active_boost_returned_within_window():
    db = _db()
    boost = _boost(
        db,
        "w1",
        boost_start_at=datetime.utcnow() - timedelta(days=1),
        boost_end_at=datetime.utcnow() + timedelta(days=1),
    )
    result = svc.active_boost_for_walker("w1", db)
    assert result is not None
    assert result.id == boost.id


def test_active_boost_expires_and_persists_state():
    db = _db()
    boost = _boost(
        db,
        "w1",
        boost_end_at=datetime.utcnow() - timedelta(days=1),
    )
    result = svc.active_boost_for_walker("w1", db)
    assert result is None
    # side effect: status becomes expired and disabled, persisted to DB
    db.refresh(boost)
    assert boost.boost_status == "expired"
    assert boost.boost_enabled is False


def test_active_boost_picks_most_recently_updated():
    db = _db()
    old = _boost(
        db,
        "w1",
        boost_status="active",
        updated_at=datetime.utcnow() - timedelta(days=10),
    )
    new = _boost(
        db,
        "w1",
        boost_status="active",
        updated_at=datetime.utcnow(),
    )
    result = svc.active_boost_for_walker("w1", db)
    assert result is not None
    assert result.id == new.id
    assert result.id != old.id
