from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.tip_integrity_flag import TipIntegrityFlag
from app.services import tip_integrity_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[TipIntegrityFlag.__table__])
    return sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# create_tip_flag
# ---------------------------------------------------------------------------


def test_create_tip_flag_defaults():
    db = _db()
    flag = svc.create_tip_flag(walker_id="w1", tip_amount=200.0, db=db)

    assert flag.id is not None
    assert flag.walker_id == "w1"
    assert flag.tip_amount == 200.0
    # Default values defined by the service signature.
    assert flag.flag_type == "unusually_high_tip"
    assert flag.severity == "medium"
    assert flag.status == "open"
    assert flag.tutor_id is None
    assert flag.walk_id is None
    assert flag.notes is None
    assert flag.created_at is not None
    assert flag.reviewed_at is None


def test_create_tip_flag_custom_fields_persisted():
    db = _db()
    flag = svc.create_tip_flag(
        walker_id="w1",
        tip_amount=50.0,
        db=db,
        flag_type="repeated_tipper",
        severity="high",
        tutor_id="t1",
        walk_id="walk1",
        notes="suspeito",
    )

    # Re-read from DB to confirm it was committed.
    fetched = db.get(TipIntegrityFlag, flag.id)
    assert fetched is not None
    assert fetched.flag_type == "repeated_tipper"
    assert fetched.severity == "high"
    assert fetched.tutor_id == "t1"
    assert fetched.walk_id == "walk1"
    assert fetched.notes == "suspeito"
    assert fetched.status == "open"


# ---------------------------------------------------------------------------
# review_tip_flag
# ---------------------------------------------------------------------------


def test_review_tip_flag_not_found_raises_404():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        svc.review_tip_flag("missing-id", "reviewed", None, db)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("status", ["reviewed", "dismissed", "confirmed"])
def test_review_tip_flag_terminal_status_sets_reviewed_at(status):
    db = _db()
    flag = svc.create_tip_flag(walker_id="w1", tip_amount=100.0, db=db)
    assert flag.reviewed_at is None

    reviewed = svc.review_tip_flag(flag.id, status, "ok", db)

    assert reviewed.status == status
    assert reviewed.notes == "ok"
    assert isinstance(reviewed.reviewed_at, datetime)


def test_review_tip_flag_non_terminal_status_does_not_set_reviewed_at():
    db = _db()
    flag = svc.create_tip_flag(walker_id="w1", tip_amount=100.0, db=db)

    reviewed = svc.review_tip_flag(flag.id, "in_progress", None, db)

    # Non-terminal status: status changes but reviewed_at stays None.
    assert reviewed.status == "in_progress"
    assert reviewed.reviewed_at is None


def test_review_tip_flag_keeps_existing_notes_when_none_passed():
    db = _db()
    flag = svc.create_tip_flag(walker_id="w1", tip_amount=100.0, db=db, notes="original")

    reviewed = svc.review_tip_flag(flag.id, "reviewed", None, db)

    # notes=None should not overwrite the existing note (`notes or flag.notes`).
    assert reviewed.notes == "original"


def test_review_tip_flag_overwrites_notes_when_provided():
    db = _db()
    flag = svc.create_tip_flag(walker_id="w1", tip_amount=100.0, db=db, notes="original")

    reviewed = svc.review_tip_flag(flag.id, "reviewed", "novo", db)

    assert reviewed.notes == "novo"


# ---------------------------------------------------------------------------
# evaluate_tip_patterns (MVP: no reputation effect, just returns flags)
# ---------------------------------------------------------------------------


def test_evaluate_tip_patterns_empty():
    db = _db()
    assert svc.evaluate_tip_patterns("w1", db) == []


def test_evaluate_tip_patterns_returns_only_matching_walker():
    db = _db()
    svc.create_tip_flag(walker_id="w1", tip_amount=10.0, db=db)
    svc.create_tip_flag(walker_id="w1", tip_amount=20.0, db=db)
    svc.create_tip_flag(walker_id="w2", tip_amount=30.0, db=db)

    result = svc.evaluate_tip_patterns("w1", db)

    assert len(result) == 2
    assert all(f.walker_id == "w1" for f in result)


def test_evaluate_tip_patterns_does_not_mutate_flags():
    db = _db()
    flag = svc.create_tip_flag(walker_id="w1", tip_amount=10.0, db=db)

    result = svc.evaluate_tip_patterns("w1", db)

    # MVP guarantee: evaluation does not touch reputation/status/severity.
    assert len(result) == 1
    assert result[0].id == flag.id
    assert result[0].status == "open"
    assert result[0].severity == "medium"
    assert result[0].reviewed_at is None


# ---------------------------------------------------------------------------
# tip_flag_payload
# ---------------------------------------------------------------------------


def test_tip_flag_payload_shape():
    db = _db()
    flag = svc.create_tip_flag(walker_id="w1", tip_amount=10.0, db=db, tutor_id="t1")

    payload = svc.tip_flag_payload(flag)

    assert payload["id"] == flag.id
    assert payload["walker_id"] == "w1"
    assert payload["tutor_id"] == "t1"
    assert payload["tip_amount"] == 10.0
    assert payload["status"] == "open"
    assert set(payload.keys()) == {
        "id",
        "walker_id",
        "tutor_id",
        "walk_id",
        "tip_amount",
        "flag_type",
        "severity",
        "status",
        "notes",
        "created_at",
        "reviewed_at",
    }
