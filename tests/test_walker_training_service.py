"""S2 — serviço da Capacitação: gabarito nunca exposto, >=80% aprova, conclusão só com nacionais."""
import json

import pytest
from fastapi import HTTPException

from app.models.walker_profile import WalkerProfile
from app.models.walker_training_progress import WalkerTrainingProgress
from app.services import walker_training_service as svc
from tests.training_helpers import (
    M01_ANSWERS, M08_ANSWERS, M10B_ANSWERS, WALKER_ID,
    configure_training, make_bundle, make_db, write_bundle,
)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    write_bundle(tmp_path, make_bundle(status="draft"))
    configure_training(monkeypatch, tmp_path, mode="warn")
    session = make_db()
    yield session
    session.close()


def _dump(value) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def test_summary_without_bundle_is_unavailable(tmp_path, monkeypatch):
    configure_training(monkeypatch, tmp_path / "vazio", mode="warn")
    session = make_db()
    summary = svc.training_summary(session, WALKER_ID)
    assert summary["available"] is False
    assert summary["modules"] == []


def test_summary_lists_national_modules_without_answer_key(db):
    summary = svc.training_summary(db, WALKER_ID)
    assert summary["available"] is True
    assert summary["version"] == "9.0"
    assert summary["content_status"] == "draft"
    assert summary["pass_threshold"] == 80
    assert summary["enforcement"]["mode"] == "warn"
    assert summary["enforcement"]["blocking"] is False
    assert [m["id"] for m in summary["modules"]] == ["m01", "m08"]
    assert summary["modules"][0]["question_count"] == 5
    assert summary["completed"] is False
    dumped = _dump(summary)
    assert "correct_index" not in dumped and "explanation" not in dumped


def test_module_detail_hides_answer_key(db):
    detail = svc.module_detail(db, WALKER_ID, "m01")
    assert detail["title"] == "Passeio seguro"
    assert detail["sections"][0]["markdown"] == "> [!FACA] Faça certo."
    assert all(set(q) == {"index", "question", "options"} for q in detail["quiz"])
    dumped = _dump(detail)
    assert "correct_index" not in dumped and "explanation" not in dumped


def test_module_detail_unknown_is_404(db):
    with pytest.raises(HTTPException) as exc:
        svc.module_detail(db, WALKER_ID, "m99")
    assert exc.value.status_code == 404


def test_grade_quiz_80_percent_passes_without_exposing_index(db):
    answers = list(M01_ANSWERS)
    answers[4] = (answers[4] + 1) % 4  # 4 de 5 certas
    result = svc.grade_quiz(db, WALKER_ID, "m01", answers)
    assert result["score"] == 80
    assert result["hits"] == 4
    assert result["passed"] is True
    assert [r["correct"] for r in result["results"]] == [True, True, True, True, False]
    assert result["results"][4]["explanation"] == "Explicação 4."
    assert "correct_index" not in _dump(result)
    assert result["progress"]["passed"] is True
    assert result["training_completed"] is False


def test_grade_quiz_below_threshold_keeps_best_score_and_counts_attempts(db):
    wrong = [(a + 1) % 4 for a in M01_ANSWERS]
    three_right = list(M01_ANSWERS[:3]) + wrong[3:]
    first = svc.grade_quiz(db, WALKER_ID, "m01", three_right)
    assert first["score"] == 60 and first["passed"] is False
    second = svc.grade_quiz(db, WALKER_ID, "m01", wrong)
    assert second["score"] == 0
    row = db.query(WalkerTrainingProgress).filter_by(walker_user_id=WALKER_ID, module_id="m01").one()
    assert row.attempts == 2
    assert row.best_score == 60
    assert row.passed_at is None
    assert row.content_version == "9.0"


def test_grade_quiz_validates_answers(db):
    with pytest.raises(HTTPException) as short:
        svc.grade_quiz(db, WALKER_ID, "m01", [0, 1])
    assert short.value.status_code == 422
    with pytest.raises(HTTPException) as out_of_range:
        svc.grade_quiz(db, WALKER_ID, "m01", [0, 1, 2, 3, 9])
    assert out_of_range.value.status_code == 422


def test_completion_requires_all_national_modules_only(db):
    assert svc.grade_quiz(db, WALKER_ID, "m10b-salvador", M10B_ANSWERS)["training_completed"] is False
    assert svc.grade_quiz(db, WALKER_ID, "m01", M01_ANSWERS)["training_completed"] is False
    result = svc.grade_quiz(db, WALKER_ID, "m08", M08_ANSWERS)
    assert result["training_completed"] is True
    profile = db.query(WalkerProfile).filter_by(user_id=WALKER_ID).one()
    assert profile.training_completed_version == "9.0"
    assert profile.training_completed_at is not None
    summary = svc.training_summary(db, WALKER_ID)
    assert summary["completed"] is True
    assert summary["passed_count"] == 2


def test_quick_guide_returns_cards_without_quiz(db):
    guide = svc.quick_guide()
    assert guide["version"] == "9.0"
    assert [c["title"] for c in guide["cards"]] == ["8.4 Sangramento", "8.7 Insolação"]
    assert guide["cards"][0]["module_id"] == "m08"
    assert "quiz" not in _dump(guide)
