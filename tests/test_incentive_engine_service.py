from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_profile import WalkerProfile
from app.services import incentive_engine_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WalkerIncentive.__table__,
            WalkerProfile.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _incentive(db, *, walker_id="w1", incentive_type="badge", title="T", status="active",
               expires_at=None, source="system", visibility_effect="none", created_at=None):
    inc = WalkerIncentive(
        id=uuid4().hex,
        walker_id=walker_id,
        incentive_type=incentive_type,
        title=title,
        description="desc",
        source=source,
        status=status,
        visibility_effect=visibility_effect,
        expires_at=expires_at,
    )
    if created_at is not None:
        inc.created_at = created_at
    db.add(inc)
    db.commit()
    db.refresh(inc)
    return inc


def _profile(db, *, user_id="w1", status="approved"):
    p = WalkerProfile(id=uuid4().hex, user_id=user_id, status=status)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


# ---------------------------------------------------------------------------
# incentive_payload
# ---------------------------------------------------------------------------

def test_incentive_payload_contains_all_fields():
    db = _db()
    inc = _incentive(db, title="Top", source="reputation", visibility_effect="low")
    payload = svc.incentive_payload(inc)
    assert payload["id"] == inc.id
    assert payload["walker_id"] == "w1"
    assert payload["incentive_type"] == "badge"
    assert payload["title"] == "Top"
    assert payload["source"] == "reputation"
    assert payload["status"] == "active"
    assert payload["visibility_effect"] == "low"
    # nullable fields present even when None
    assert "revoked_at" in payload
    assert "admin_notes" in payload
    assert payload["revoked_at"] is None


# ---------------------------------------------------------------------------
# expire_incentives
# ---------------------------------------------------------------------------

def test_expire_incentives_marks_past_active_as_expired():
    db = _db()
    past = datetime.utcnow() - timedelta(days=1)
    inc = _incentive(db, status="active", expires_at=past)
    svc.expire_incentives("w1", db)
    db.refresh(inc)
    assert inc.status == "expired"


def test_expire_incentives_ignores_future_expiry():
    db = _db()
    future = datetime.utcnow() + timedelta(days=1)
    inc = _incentive(db, status="active", expires_at=future)
    svc.expire_incentives("w1", db)
    db.refresh(inc)
    assert inc.status == "active"


def test_expire_incentives_ignores_null_expiry():
    db = _db()
    inc = _incentive(db, status="active", expires_at=None)
    svc.expire_incentives("w1", db)
    db.refresh(inc)
    assert inc.status == "active"


def test_expire_incentives_only_touches_active_status():
    db = _db()
    past = datetime.utcnow() - timedelta(days=1)
    inc = _incentive(db, status="revoked", expires_at=past)
    svc.expire_incentives("w1", db)
    db.refresh(inc)
    assert inc.status == "revoked"


def test_expire_incentives_scoped_by_walker():
    db = _db()
    past = datetime.utcnow() - timedelta(days=1)
    mine = _incentive(db, walker_id="w1", status="active", expires_at=past)
    other = _incentive(db, walker_id="w2", status="active", expires_at=past)
    svc.expire_incentives("w1", db)
    db.refresh(mine)
    db.refresh(other)
    assert mine.status == "expired"
    assert other.status == "active"


# ---------------------------------------------------------------------------
# get_active_incentives
# ---------------------------------------------------------------------------

def test_get_active_incentives_returns_only_active_and_expires_first():
    db = _db()
    past = datetime.utcnow() - timedelta(days=1)
    future = datetime.utcnow() + timedelta(days=5)
    expired_one = _incentive(db, title="will-expire", status="active", expires_at=past)
    active_one = _incentive(db, title="stays", status="active", expires_at=future)
    revoked = _incentive(db, title="revoked", status="revoked")

    result = svc.get_active_incentives("w1", db)
    ids = {r.id for r in result}
    assert active_one.id in ids
    assert expired_one.id not in ids  # got expired by side effect
    assert revoked.id not in ids
    # side effect persisted
    db.refresh(expired_one)
    assert expired_one.status == "expired"


def test_get_active_incentives_ordered_by_created_desc():
    db = _db()
    old = _incentive(db, title="old", created_at=datetime(2020, 1, 1))
    new = _incentive(db, title="new", created_at=datetime(2024, 1, 1))
    result = svc.get_active_incentives("w1", db)
    assert [r.id for r in result] == [new.id, old.id]


# ---------------------------------------------------------------------------
# grant_incentive
# ---------------------------------------------------------------------------

def test_grant_incentive_creates_active_with_default_expiry():
    db = _db()
    before = datetime.utcnow()
    inc = svc.grant_incentive("w1", "badge", "Bom", "desc", "reputation", db)
    assert inc.status == "active"
    assert inc.id is not None
    assert inc.granted_at is not None
    # default expiry ~7 days out
    assert inc.expires_at is not None
    delta = inc.expires_at - before
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_grant_incentive_respects_explicit_expiry_and_notes():
    db = _db()
    exp = datetime.utcnow() + timedelta(days=30)
    inc = svc.grant_incentive(
        "w1", "badge", "Bom", "desc", "reputation", db,
        visibility_effect="medium", expires_at=exp, admin_notes="manual",
    )
    assert inc.expires_at == exp
    assert inc.visibility_effect == "medium"
    assert inc.admin_notes == "manual"


def test_grant_incentive_is_idempotent_for_active_duplicate():
    db = _db()
    first = svc.grant_incentive("w1", "badge", "Bom", "desc", "reputation", db)
    second = svc.grant_incentive("w1", "badge", "Bom", "outra desc", "reputation", db)
    assert first.id == second.id
    count = db.query(WalkerIncentive).filter_by(walker_id="w1", title="Bom").count()
    assert count == 1


def test_grant_incentive_dedupes_against_pending():
    db = _db()
    existing = _incentive(db, incentive_type="badge", title="Bom", status="pending")
    result = svc.grant_incentive("w1", "badge", "Bom", "desc", "reputation", db)
    assert result.id == existing.id


def test_grant_incentive_allows_new_after_revoked():
    db = _db()
    _incentive(db, incentive_type="badge", title="Bom", status="revoked")
    result = svc.grant_incentive("w1", "badge", "Bom", "desc", "reputation", db)
    assert result.status == "active"
    count = db.query(WalkerIncentive).filter_by(walker_id="w1", title="Bom").count()
    assert count == 2


def test_grant_incentive_dedup_scoped_by_title_and_type():
    db = _db()
    a = svc.grant_incentive("w1", "badge", "TitleA", "d", "s", db)
    b = svc.grant_incentive("w1", "badge", "TitleB", "d", "s", db)
    assert a.id != b.id


# ---------------------------------------------------------------------------
# revoke_incentive
# ---------------------------------------------------------------------------

def test_revoke_incentive_sets_revoked_state():
    db = _db()
    inc = _incentive(db, status="active")
    revoked = svc.revoke_incentive(inc.id, db, admin_notes="abuso")
    assert revoked.status == "revoked"
    assert revoked.revoked_at is not None
    assert revoked.admin_notes == "abuso"


def test_revoke_incentive_keeps_existing_notes_when_none_passed():
    db = _db()
    inc = _incentive(db, status="active")
    inc.admin_notes = "nota original"
    db.commit()
    revoked = svc.revoke_incentive(inc.id, db)
    assert revoked.admin_notes == "nota original"


def test_revoke_incentive_missing_raises_404():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        svc.revoke_incentive("nonexistent", db)
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# list_incentives
# ---------------------------------------------------------------------------

def test_list_incentives_returns_all_statuses_and_expires():
    db = _db()
    past = datetime.utcnow() - timedelta(days=1)
    active_exp = _incentive(db, title="a", status="active", expires_at=past, created_at=datetime(2024, 1, 1))
    revoked = _incentive(db, title="b", status="revoked", created_at=datetime(2023, 1, 1))
    result = svc.list_incentives("w1", db)
    assert {r.id for r in result} == {active_exp.id, revoked.id}
    # ordered by created desc
    assert [r.id for r in result] == [active_exp.id, revoked.id]
    # active+past got expired as side effect
    db.refresh(active_exp)
    assert active_exp.status == "expired"


# ---------------------------------------------------------------------------
# evaluate_incentives (dependencies monkeypatched)
# ---------------------------------------------------------------------------

def _patch_deps(monkeypatch, *, scores, summary, mission):
    monkeypatch.setattr(svc, "calculate_hybrid_reputation_score", lambda wid, db: scores)
    monkeypatch.setattr(svc, "reputation_summary", lambda wid, db: summary)
    monkeypatch.setattr(svc, "get_walker_mission_summary", lambda wid, db: mission)


def test_evaluate_no_profile_returns_active_without_granting(monkeypatch):
    db = _db()
    # no profile created
    called = {"hybrid": False}

    def boom(*a, **k):
        called["hybrid"] = True
        raise AssertionError("should not be called")

    monkeypatch.setattr(svc, "calculate_hybrid_reputation_score", boom)
    result = svc.evaluate_incentives("w1", db)
    assert result == []
    assert called["hybrid"] is False


def test_evaluate_unapproved_profile_returns_active_without_granting(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="pending")
    monkeypatch.setattr(svc, "calculate_hybrid_reputation_score",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    result = svc.evaluate_incentives("w1", db)
    assert result == []


@pytest.mark.parametrize("risk", ["risk", "critical", "suspended"])
def test_evaluate_high_risk_skips_grants(monkeypatch, risk):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    monkeypatch.setattr(svc, "calculate_hybrid_reputation_score",
                        lambda wid, db: {"hybrid_reputation_score": 99, "risk_level": risk})
    # summary/mission should never be consulted
    monkeypatch.setattr(svc, "reputation_summary",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("skip")))
    result = svc.evaluate_incentives("w1", db)
    assert result == []
    assert db.query(WalkerIncentive).count() == 0


def test_evaluate_grants_reputation_badge(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 50, "risk_level": "normal"},
        summary={"reviews_count": 5, "rating_average": 4.8},
        mission={"completed_missions": 0},
    )
    result = svc.evaluate_incentives("w1", db)
    badges = [r for r in result if r.title == "Passeador bem avaliado"]
    assert len(badges) == 1
    assert badges[0].incentive_type == "badge"
    assert badges[0].visibility_effect == "low"


def test_evaluate_no_badge_when_below_thresholds(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 50, "risk_level": "normal"},
        summary={"reviews_count": 4, "rating_average": 5.0},  # only 4 reviews
        mission={"completed_missions": 0},
    )
    result = svc.evaluate_incentives("w1", db)
    assert all(r.title != "Passeador bem avaliado" for r in result)


def test_evaluate_grants_mission_recognition(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 50, "risk_level": "normal"},
        summary={"reviews_count": 0, "rating_average": 0.0},
        mission={"completed_missions": 3},
    )
    result = svc.evaluate_incentives("w1", db)
    rec = [r for r in result if r.title == "Evolucao consistente"]
    assert len(rec) == 1
    assert rec[0].incentive_type == "recognition"


def test_evaluate_mission_summary_missing_key_defaults_zero(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 50, "risk_level": "normal"},
        summary={"reviews_count": 0, "rating_average": 0.0},
        mission={},  # no completed_missions key -> .get default 0
    )
    result = svc.evaluate_incentives("w1", db)
    assert all(r.title != "Evolucao consistente" for r in result)


def test_evaluate_grants_visibility_boost_at_high_score(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 88, "risk_level": "normal"},
        summary={"reviews_count": 0, "rating_average": 0.0},
        mission={"completed_missions": 0},
    )
    result = svc.evaluate_incentives("w1", db)
    boost = [r for r in result if r.title == "Destaque da semana"]
    assert len(boost) == 1
    assert boost[0].incentive_type == "visibility_boost"
    assert boost[0].visibility_effect == "medium"


def test_evaluate_no_boost_below_88(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 87.99, "risk_level": "normal"},
        summary={"reviews_count": 0, "rating_average": 0.0},
        mission={"completed_missions": 0},
    )
    result = svc.evaluate_incentives("w1", db)
    assert all(r.title != "Destaque da semana" for r in result)


def test_evaluate_no_boost_when_risk_not_normal_even_if_below_threshold(monkeypatch):
    # risk_level "attention" passes the early-return gate (not in risk/critical/suspended)
    # but the visibility boost requires risk_level == "normal"
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 95, "risk_level": "attention"},
        summary={"reviews_count": 0, "rating_average": 0.0},
        mission={"completed_missions": 0},
    )
    result = svc.evaluate_incentives("w1", db)
    assert all(r.title != "Destaque da semana" for r in result)


def test_evaluate_grants_all_three_when_qualified(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 90, "risk_level": "normal"},
        summary={"reviews_count": 10, "rating_average": 5.0},
        mission={"completed_missions": 5},
    )
    result = svc.evaluate_incentives("w1", db)
    titles = {r.title for r in result}
    assert {"Passeador bem avaliado", "Evolucao consistente", "Destaque da semana"} <= titles
    assert db.query(WalkerIncentive).filter_by(status="active").count() == 3


def test_evaluate_is_idempotent_across_runs(monkeypatch):
    db = _db()
    _profile(db, user_id="w1", status="approved")
    _patch_deps(
        monkeypatch,
        scores={"hybrid_reputation_score": 90, "risk_level": "normal"},
        summary={"reviews_count": 10, "rating_average": 5.0},
        mission={"completed_missions": 5},
    )
    svc.evaluate_incentives("w1", db)
    svc.evaluate_incentives("w1", db)
    # dedup prevents duplicates
    assert db.query(WalkerIncentive).count() == 3
