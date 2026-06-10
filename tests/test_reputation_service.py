"""Testes de unidade para app/services/reputation_service.py.

Cobre o comportamento REAL das funcoes de reputacao:
- walker_level (cortes por nivel)
- completed_walks_count
- calculate_basic_behavior_score (cancellation_rate, scores)
- calculate_hybrid_reputation_score (pesos + penalidade)
- determine_risk_level (suspended / critical / risk / attention / normal)

Banco SQLite em memoria; nenhuma dependencia de app.main, alembic ou banco real.
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.services import reputation_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Walk.__table__,
            WalkerReview.__table__,
            WalkerProfile.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _walk(db, walker_id, status, *, created_at=None):
    walk = Walk(
        id=str(uuid4()),
        tutor_id="tutor1",
        walker_id=walker_id,
        pet_id="pet1",
        scheduled_date="2026-06-09",
        duration_minutes=30,
        price=50.0,
        status=status,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(walk)
    db.commit()
    return walk


def _review(db, walker_id, rating, *, flagged=False):
    review = WalkerReview(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        tutor_id="tutor1",
        walker_id=walker_id,
        rating=rating,
        is_flagged=flagged,
    )
    db.add(review)
    db.commit()
    return review


def _profile(db, walker_id, status):
    profile = WalkerProfile(id=str(uuid4()), user_id=walker_id, status=status)
    db.add(profile)
    db.commit()
    return profile


# ----------------------------- walker_level -----------------------------

def test_walker_level_zero_reviews_is_iniciante():
    # Sem avaliacoes => Iniciante independentemente de passeios/rating.
    assert svc.walker_level(total_walks=100, rating_average=5.0, reviews_count=0) == "Iniciante"


def test_walker_level_elite():
    assert svc.walker_level(80, 4.85, 5) == "Elite Aumigao"


def test_walker_level_elite_boundary_below_rating_drops_to_destaque():
    # 80 passeios mas rating 4.84 (< 4.85) nao alcanca Elite; cai para Destaque.
    assert svc.walker_level(80, 4.84, 5) == "Destaque"


def test_walker_level_destaque():
    assert svc.walker_level(30, 4.7, 5) == "Destaque"


def test_walker_level_confiavel():
    assert svc.walker_level(10, 4.5, 5) == "Confiavel"


def test_walker_level_below_all_cuts_is_iniciante():
    assert svc.walker_level(9, 4.9, 5) == "Iniciante"
    assert svc.walker_level(30, 4.69, 5) == "Confiavel"


# ------------------------- completed_walks_count -------------------------

def test_completed_walks_count_counts_only_completed_statuses():
    db = _db()
    _walk(db, "w1", "Finalizado")
    _walk(db, "w1", "Concluido")
    _walk(db, "w1", "completed")
    _walk(db, "w1", "Agendado")  # nao conta
    _walk(db, "w1", "cancelado")  # nao conta
    _walk(db, "w2", "Finalizado")  # outro walker
    assert svc.completed_walks_count("w1", db) == 3


def test_completed_walks_count_empty():
    db = _db()
    assert svc.completed_walks_count("w1", db) == 0


# ------------------- calculate_basic_behavior_score ----------------------

def test_behavior_score_no_walks_returns_defaults():
    db = _db()
    result = svc.calculate_basic_behavior_score("w1", db)
    assert result == {
        "behavior_score": 75.0,
        "acceptance_rate_score": 75.0,
        "cancellation_score": 75.0,
        "activity_score": 75.0,
        "cancellation_rate": 0.0,
    }


def test_behavior_score_cancellation_rate_and_scores():
    db = _db()
    # 1 cancelado de 4 totais => cancellation_rate 25%.
    # Completados em 3 dias distintos => activity_score = 3*12 = 36.
    _walk(db, "w1", "Finalizado", created_at=datetime(2026, 6, 1, 10))
    _walk(db, "w1", "Finalizado", created_at=datetime(2026, 6, 2, 10))
    _walk(db, "w1", "Finalizado", created_at=datetime(2026, 6, 3, 10))
    _walk(db, "w1", "cancelado", created_at=datetime(2026, 6, 4, 10))

    result = svc.calculate_basic_behavior_score("w1", db)
    assert result["cancellation_rate"] == 25.0
    assert result["acceptance_rate_score"] == 82.0
    # cancellation_score = 100 - 25 = 75
    assert result["cancellation_score"] == 75.0
    # activity_score = 3 dias * 12 = 36
    assert result["activity_score"] == 36.0
    # behavior_score = 82*0.40 + 75*0.40 + 36*0.20 = 32.8 + 30 + 7.2 = 70.0
    assert result["behavior_score"] == 70.0


def test_behavior_score_activity_capped_at_100():
    db = _db()
    # 10 dias distintos completados => 10*12 = 120, clamp para 100.
    for day in range(1, 11):
        _walk(db, "w1", "Finalizado", created_at=datetime(2026, 6, day, 9))
    result = svc.calculate_basic_behavior_score("w1", db)
    assert result["activity_score"] == 100.0
    assert result["cancellation_rate"] == 0.0
    assert result["cancellation_score"] == 100.0


def test_behavior_score_only_cancelled_no_active_days_fallback():
    db = _db()
    # So passeios cancelados: nenhum completado => active_days = 0 => activity_score 75.0 (fallback).
    _walk(db, "w1", "cancelado")
    _walk(db, "w1", "cancelado")
    result = svc.calculate_basic_behavior_score("w1", db)
    assert result["cancellation_rate"] == 100.0
    assert result["cancellation_score"] == 0.0
    assert result["activity_score"] == 75.0
    # behavior = 82*0.40 + 0*0.40 + 75*0.20 = 32.8 + 0 + 15 = 47.8
    assert result["behavior_score"] == 47.8


# ------------------- calculate_hybrid_reputation_score -------------------

def test_hybrid_score_no_data_uses_defaults():
    db = _db()
    result = svc.calculate_hybrid_reputation_score("w1", db)
    # rating_score=75 (sem reviews), experience_score=40 (<5 walks), behavior=75 (sem walks),
    # risk_penalty=0 => 75*0.70 + 40*0.20 + 75*0.10 - 0 = 52.5 + 8 + 7.5 = 68.0
    assert result["rating_score"] == 75.0
    assert result["experience_score"] == 40.0
    assert result["behavior_score"] == 75.0
    assert result["risk_penalty"] == 0.0
    assert result["hybrid_reputation_score"] == 68.0
    assert result["recent_rating_score"] is None
    # consistency_score espelha activity_score do behavior (75 sem walks)
    assert result["consistency_score"] == 75.0
    assert result["risk_level"] == "normal"
    assert result["behavior_details"]["cancellation_rate"] == 0.0


def test_hybrid_score_risk_penalty_capped_at_25():
    db = _db()
    # 6 reviews flagged => penalty 6*5 = 30, capada em 25.
    for _ in range(6):
        _review(db, "w1", 5, flagged=True)
    result = svc.calculate_hybrid_reputation_score("w1", db)
    assert result["risk_penalty"] == 25.0


def test_hybrid_score_high_performer():
    db = _db()
    # 80 passeios completados => experience_score 100.
    for day in range(1, 11):
        for _ in range(8):
            _walk(db, "w1", "Finalizado", created_at=datetime(2026, 6, day, 9))
    # reviews 5 estrelas => rating_average 5 => rating_score 100.
    for _ in range(5):
        _review(db, "w1", 5)
    result = svc.calculate_hybrid_reputation_score("w1", db)
    assert result["rating_score"] == 100.0
    assert result["experience_score"] == 100.0
    # behavior: 0 cancelados, activity capada 100 => 82*0.4 + 100*0.4 + 100*0.2 = 32.8+40+20 = 92.8
    assert result["behavior_score"] == 92.8
    # hybrid = 100*0.7 + 100*0.2 + 92.8*0.1 = 70 + 20 + 9.28 = 99.28
    assert result["hybrid_reputation_score"] == 99.28


# ------------------------ determine_risk_level ---------------------------

def test_risk_level_suspended_profile():
    db = _db()
    _profile(db, "w1", "suspended")
    assert svc.determine_risk_level("w1", db) == "suspended"


def test_risk_level_blocked_profile():
    db = _db()
    _profile(db, "w1", "blocked")
    assert svc.determine_risk_level("w1", db) == "suspended"


def test_risk_level_critical_by_flagged():
    db = _db()
    for _ in range(3):
        _review(db, "w1", 5, flagged=True)
    assert svc.determine_risk_level("w1", db) == "critical"


def test_risk_level_critical_by_low_rating():
    db = _db()
    # 3 reviews com media < 4.0.
    _review(db, "w1", 3)
    _review(db, "w1", 4)
    _review(db, "w1", 3)  # media 3.33
    assert svc.determine_risk_level("w1", db) == "critical"


def test_risk_level_risk_by_rating_band():
    db = _db()
    # media entre 4.0 e 4.3 (4 reviews) => "risk".
    _review(db, "w1", 4)
    _review(db, "w1", 4)
    _review(db, "w1", 4)
    _review(db, "w1", 5)  # media 4.25
    assert svc.determine_risk_level("w1", db) == "risk"


def test_risk_level_risk_by_cancellation_rate():
    db = _db()
    # cancellation_rate >= 25 (sem reviews ruins) => "risk".
    _walk(db, "w1", "Finalizado")
    _walk(db, "w1", "cancelado")  # 50% cancelamento
    behavior = svc.calculate_basic_behavior_score("w1", db)
    assert behavior["cancellation_rate"] == 50.0
    assert svc.determine_risk_level("w1", db, behavior=behavior) == "risk"


def test_risk_level_attention_by_rating_band():
    db = _db()
    # media entre 4.3 e 4.6 (3+ reviews) => "attention".
    _review(db, "w1", 4)
    _review(db, "w1", 5)
    _review(db, "w1", 5)  # media 4.67? -> recompute
    # ajuste: 4,4,5 => media 4.33
    db.query(WalkerReview).delete()
    db.commit()
    _review(db, "w1", 4)
    _review(db, "w1", 4)
    _review(db, "w1", 5)  # media 4.33 (>=4.3, <4.6)
    assert svc.determine_risk_level("w1", db) == "attention"


def test_risk_level_attention_by_low_hybrid_score():
    db = _db()
    # Sem reviews/walks: rating bom, mas hybrid_score baixo forca "attention".
    assert svc.determine_risk_level("w1", db, hybrid_score=50.0) == "attention"


def test_risk_level_normal():
    db = _db()
    # 3 reviews com media alta (4.8) e sem cancelamentos => normal.
    _review(db, "w1", 5)
    _review(db, "w1", 5)
    _review(db, "w1", 4)  # media 4.67 (>=4.6)
    assert svc.determine_risk_level("w1", db, hybrid_score=90.0) == "normal"


def test_risk_level_low_rating_with_few_reviews_not_critical():
    db = _db()
    # Apenas 2 reviews ruins: o corte exige reviews_count >= 3, entao nao e critical/risk
    # por rating. Sem cancelamentos e hybrid alto => normal.
    _review(db, "w1", 1)
    _review(db, "w1", 1)
    assert svc.determine_risk_level("w1", db, hybrid_score=90.0) == "normal"
