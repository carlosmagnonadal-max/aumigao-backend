"""Testes de unidade para app/services/recovery_service.py.

Usa SQLite em memoria; cria apenas as tabelas necessarias (FK nao sao
forcadas por padrao no SQLite). Testa o comportamento REAL do servico.
"""
from datetime import datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walker_profile import WalkerProfile
from app.models.walker_recovery_plan import WalkerRecoveryPlan
from app.models.walker_review import WalkerReview
from app.services import recovery_service as svc


WALKER = "walker-1"


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WalkerRecoveryPlan.__table__,
            WalkerReview.__table__,
            Walk.__table__,
            WalkerProfile.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _review(db, walker_id=WALKER, *, rating=5, flagged=False):
    review = WalkerReview(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        tutor_id="tutor-1",
        walker_id=walker_id,
        rating=rating,
        is_flagged=flagged,
    )
    db.add(review)
    db.commit()
    return review


def _walk(db, walker_id=WALKER, *, status="Finalizado"):
    walk = Walk(
        id=str(uuid4()),
        tutor_id="tutor-1",
        walker_id=walker_id,
        pet_id="pet-1",
        scheduled_date="2026-06-01",
        duration_minutes=30,
        price=50.0,
        status=status,
        created_at=datetime.utcnow(),
    )
    db.add(walk)
    db.commit()
    return walk


# ---------------------------------------------------------------------------
# recovery_payload
# ---------------------------------------------------------------------------

def test_recovery_payload_maps_all_fields():
    plan = WalkerRecoveryPlan(
        id="p1",
        walker_id=WALKER,
        risk_level_at_start="attention",
        status="active",
        reason="motivo",
        recommended_actions=["a", "b"],
        started_at=datetime(2026, 1, 1),
        ends_at=datetime(2026, 1, 22),
        completed_at=None,
        created_at=datetime(2026, 1, 1),
        updated_at=datetime(2026, 1, 1),
    )
    payload = svc.recovery_payload(plan)
    assert payload["id"] == "p1"
    assert payload["walker_id"] == WALKER
    assert payload["risk_level_at_start"] == "attention"
    assert payload["status"] == "active"
    assert payload["reason"] == "motivo"
    assert payload["recommended_actions"] == ["a", "b"]
    assert payload["completed_at"] is None


def test_recovery_payload_none_actions_becomes_empty_list():
    plan = WalkerRecoveryPlan(id="p2", walker_id=WALKER, recommended_actions=None)
    payload = svc.recovery_payload(plan)
    assert payload["recommended_actions"] == []


# ---------------------------------------------------------------------------
# build_recommendations
# ---------------------------------------------------------------------------

def test_build_recommendations_no_reviews_first_walks_message():
    db = _db()
    recs = svc.build_recommendations(WALKER, db)
    # reviews_count == 0 dispara a mensagem de primeiros passeios; como ja
    # ha pelo menos uma recomendacao, os defaults NAO sao adicionados.
    assert recs == ["Complete seus primeiros passeios para comecar a construir sua reputacao."]


def test_build_recommendations_defaults_when_healthy():
    db = _db()
    # 3 reviews com nota alta (>=4.6) e nenhum walk cancelado => nada dispara
    for _ in range(3):
        _review(db, rating=5)
    recs = svc.build_recommendations(WALKER, db)
    assert recs == svc.DEFAULT_RECOMMENDATIONS[:3]
    assert len(recs) == 3


def test_build_recommendations_low_rating_triggers_review_tip():
    db = _db()
    # 3 reviews com media < 4.6 (4,4,4 => 4.0) e reviews_count >= 3
    for _ in range(3):
        _review(db, rating=4)
    recs = svc.build_recommendations(WALKER, db)
    assert "Revise os comentarios recentes e escolha um ponto de melhoria por passeio." in recs
    # como a regra disparou, defaults nao entram
    assert svc.DEFAULT_RECOMMENDATIONS[0] not in recs


def test_build_recommendations_high_cancellation_triggers_tip():
    db = _db()
    # rating alto para nao disparar a regra de rating; cancelamento >= 12%
    for _ in range(3):
        _review(db, rating=5)
    # 8 passeios finalizados + 2 cancelados => cancellation_rate = 20% >= 12
    for _ in range(8):
        _walk(db, status="Finalizado")
    for _ in range(2):
        _walk(db, status="cancelado")
    recs = svc.build_recommendations(WALKER, db)
    assert "Evite aceitar passeios quando houver risco de conflito de horario." in recs


# ---------------------------------------------------------------------------
# active_recovery_plan
# ---------------------------------------------------------------------------

def test_active_recovery_plan_none_when_absent():
    db = _db()
    assert svc.active_recovery_plan(WALKER, db) is None


def test_active_recovery_plan_returns_only_active():
    db = _db()
    completed = WalkerRecoveryPlan(
        id="c1", walker_id=WALKER, status="completed", created_at=datetime(2026, 1, 1)
    )
    active = WalkerRecoveryPlan(
        id="a1", walker_id=WALKER, status="active", created_at=datetime(2026, 1, 2)
    )
    db.add_all([completed, active])
    db.commit()
    found = svc.active_recovery_plan(WALKER, db)
    assert found is not None
    assert found.id == "a1"


def test_active_recovery_plan_returns_latest_active():
    db = _db()
    older = WalkerRecoveryPlan(
        id="old", walker_id=WALKER, status="active", created_at=datetime(2026, 1, 1)
    )
    newer = WalkerRecoveryPlan(
        id="new", walker_id=WALKER, status="active", created_at=datetime(2026, 2, 1)
    )
    db.add_all([older, newer])
    db.commit()
    assert svc.active_recovery_plan(WALKER, db).id == "new"


# ---------------------------------------------------------------------------
# get_or_create_recovery_plan
# ---------------------------------------------------------------------------

def test_get_or_create_low_risk_no_plan_returns_none():
    db = _db()
    # walker saudavel (sem reviews/walks) => risk_level "normal" => sem criar
    assert svc.get_or_create_recovery_plan(WALKER, db) is None
    assert db.query(WalkerRecoveryPlan).count() == 0


def test_get_or_create_low_risk_returns_existing_active_plan():
    db = _db()
    existing = WalkerRecoveryPlan(
        id="ex", walker_id=WALKER, status="active", created_at=datetime(2026, 1, 1)
    )
    db.add(existing)
    db.commit()
    # risk normal, mas existe plano ativo => retorna o ativo, nao cria outro
    result = svc.get_or_create_recovery_plan(WALKER, db)
    assert result.id == "ex"
    assert db.query(WalkerRecoveryPlan).count() == 1


def test_get_or_create_creates_plan_when_at_risk():
    db = _db()
    # 3 reviews media 4.0 (< 4.3) => risk_level "risk"
    for _ in range(3):
        _review(db, rating=4)
    plan = svc.get_or_create_recovery_plan(WALKER, db)
    assert plan is not None
    assert plan.status == "active"
    assert plan.risk_level_at_start == "risk"
    assert plan.walker_id == WALKER
    # sem prazo automatico imposto (orientacao voluntaria)
    assert plan.ends_at is None
    # reason e actions default
    assert plan.reason == "Reunimos algumas sugestoes opcionais para ajudar voce quando quiser."
    assert isinstance(plan.recommended_actions, list) and plan.recommended_actions


def test_get_or_create_force_creates_even_when_healthy():
    db = _db()
    plan = svc.get_or_create_recovery_plan(WALKER, db, force=True)
    assert plan is not None
    assert plan.status == "active"
    assert plan.risk_level_at_start == "normal"


def test_get_or_create_uses_custom_reason_and_actions():
    db = _db()
    plan = svc.get_or_create_recovery_plan(
        WALKER, db, reason="custom", actions=["x", "y"], force=True
    )
    assert plan.reason == "custom"
    assert plan.recommended_actions == ["x", "y"]


def test_get_or_create_does_not_duplicate_active_plan():
    db = _db()
    first = svc.get_or_create_recovery_plan(WALKER, db, force=True)
    second = svc.get_or_create_recovery_plan(WALKER, db, force=True)
    assert first.id == second.id
    assert db.query(WalkerRecoveryPlan).count() == 1


# ---------------------------------------------------------------------------
# update_recovery_plan_status
# ---------------------------------------------------------------------------

def test_update_status_not_found_raises_404():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        svc.update_recovery_plan_status("missing", "completed", db)
    assert exc.value.status_code == 404


def test_update_status_completed_sets_completed_at():
    db = _db()
    plan = svc.get_or_create_recovery_plan(WALKER, db, force=True)
    updated = svc.update_recovery_plan_status(plan.id, "completed", db)
    assert updated.status == "completed"
    assert updated.completed_at is not None


def test_update_status_non_completed_does_not_set_completed_at():
    db = _db()
    plan = svc.get_or_create_recovery_plan(WALKER, db, force=True)
    updated = svc.update_recovery_plan_status(plan.id, "cancelled", db)
    assert updated.status == "cancelled"
    assert updated.completed_at is None


# ---------------------------------------------------------------------------
# Item 6 — Recovery plan é orientação voluntária (risco trabalhista)
# ---------------------------------------------------------------------------

def test_recovery_plan_has_no_enforced_deadline():
    """O plano criado automaticamente NÃO deve ter ends_at definido.
    O prazo de 21 dias foi removido para que não pareça meta imposta."""
    db = _db()
    for _ in range(3):
        _review(db, rating=4)
    plan = svc.get_or_create_recovery_plan(WALKER, db)
    assert plan is not None
    # ends_at deve ser None — prazo automático foi removido
    assert plan.ends_at is None, (
        f"Recovery plan não deve ter prazo automático, mas ends_at={plan.ends_at!r}"
    )


def test_recovery_plan_reason_uses_voluntary_language():
    """A razão padrão do plano deve usar linguagem de sugestão, não de
    acompanhamento/controle."""
    db = _db()
    for _ in range(3):
        _review(db, rating=4)
    plan = svc.get_or_create_recovery_plan(WALKER, db)
    assert plan is not None
    reason_lower = plan.reason.lower()
    # Não deve conter linguagem de acompanhamento obrigatório
    FORBIDDEN_TERMS = ["acompanhamento", "plano de recupera", "recuperacao obrigat", "requerida"]
    for term in FORBIDDEN_TERMS:
        assert term not in reason_lower, (
            f"Reason do plano contém linguagem de controle '{term}': {plan.reason!r}"
        )
    # Deve conter linguagem de sugestão voluntária
    VOLUNTARY_TERMS = ["sugest", "dica", "opcional", "voluntar", "quando quiser", "livre"]
    assert any(term in reason_lower for term in VOLUNTARY_TERMS), (
        f"Reason do plano não contém linguagem voluntária: {plan.reason!r}"
    )


def test_default_recommendations_are_suggestions_not_mandates():
    """As recomendações padrão devem ser sugestões, não obrigações."""
    for rec in svc.DEFAULT_RECOMMENDATIONS:
        rec_lower = rec.lower()
        # Não deve conter linguagem de obrigação
        assert "obrigat" not in rec_lower, f"Recomendação parece obrigatória: {rec!r}"
        assert "deve " not in rec_lower, f"Recomendação parece obrigatória: {rec!r}"


def test_recovery_plan_does_not_block_or_penalize():
    """O plano de recuperação é puramente informativo.
    get_or_create_recovery_plan não deve lançar exceção nem bloquear."""
    db = _db()
    # Walker saudável => sem plano
    result = svc.get_or_create_recovery_plan(WALKER, db)
    assert result is None  # sem risco, sem plano

    # Walker em risco => plano criado mas apenas informativo (não levanta exceção)
    for _ in range(3):
        _review(db, rating=4)
    plan = svc.get_or_create_recovery_plan(WALKER, db)
    assert plan is not None
    assert plan.status == "active"
    # Sem atributo de bloqueio ou consequência de acesso
    assert not hasattr(plan, "blocks_matching") or getattr(plan, "blocks_matching", False) is False
