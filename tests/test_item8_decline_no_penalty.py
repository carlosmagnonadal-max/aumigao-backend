"""ITEM 8 — Recusa de oferta não penaliza acesso ao trabalho.

Garante que:
1. Declinar/expirar uma oferta (WalkMatchingAttempt declined/expired) NÃO reduz o
   acceptance_rate_score nem o behavior_score nem o final_matching_score do passeador.
2. acceptance_rate_score é fixo/neutro independentemente de quantas ofertas foram
   recusadas — o score não é calculado a partir de recusas reais.
3. No-show (walk.status=="cancelado") CONTINUA penalizando via cancellation_score,
   pois é quebra de compromisso já aceito (sinal legítimo de confiabilidade).

Padrão do projeto: SQLite em memória, sem app.main, sem banco real.
"""
import pytest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.walk import Walk, WalkMatchingAttempt
from app.models.walker_profile import WalkerProfile
from app.models.walker_review import WalkerReview
from app.services import behavior_score_service as bsvc
from app.services import reputation_service as rsvc


# --------------------------------------------------------------------------- #
# Infra
# --------------------------------------------------------------------------- #
_seq = {"n": 0}


def _db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _walker_id():
    _seq["n"] += 1
    return f"walker-{_seq['n']}"


def _profile(db, user_id: str) -> WalkerProfile:
    _seq["n"] += 1
    profile = WalkerProfile(
        id=f"wp-{_seq['n']}",
        user_id=user_id,
        full_name="Passeador Teste",
        status="active",
        active_as_walker=True,
        created_at=datetime(2024, 1, 1),
    )
    db.add(profile)
    db.commit()
    return profile


def _walk(db, walker_id: str, status: str = "Finalizado") -> Walk:
    _seq["n"] += 1
    w = Walk(
        id=f"walk-{_seq['n']}",
        tutor_id=f"tutor-{_seq['n']}",
        walker_id=walker_id,
        pet_id=f"pet-{_seq['n']}",
        scheduled_date="2024-06-01T10:00:00",
        duration_minutes=30,
        price=50.0,
        status=status,
        created_at=datetime(2024, 6, 1),
    )
    db.add(w)
    db.commit()
    return w


def _declined_attempt(db, walk_id: str, walker_id: str) -> WalkMatchingAttempt:
    """Simula uma oferta que foi recusada pelo passeador."""
    _seq["n"] += 1
    now = datetime.utcnow()
    attempt = WalkMatchingAttempt(
        id=f"att-{_seq['n']}",
        walk_id=walk_id,
        walker_id=walker_id,
        attempt_number=1,
        status="declined",
        score=75.0,
        sent_at=now - timedelta(minutes=5),
        responded_at=now,
        expires_at=now + timedelta(minutes=25),
        response_time_seconds=300,
        reason="walker_declined",
    )
    db.add(attempt)
    db.commit()
    return attempt


def _expired_attempt(db, walk_id: str, walker_id: str) -> WalkMatchingAttempt:
    """Simula uma oferta que expirou sem resposta (passeador ignorou)."""
    _seq["n"] += 1
    now = datetime.utcnow()
    attempt = WalkMatchingAttempt(
        id=f"att-{_seq['n']}",
        walk_id=walk_id,
        walker_id=walker_id,
        attempt_number=1,
        status="expired",
        score=75.0,
        sent_at=now - timedelta(minutes=35),
        responded_at=now,
        expires_at=now - timedelta(minutes=5),
        response_time_seconds=None,
        reason="confirmation_timeout",
    )
    db.add(attempt)
    db.commit()
    return attempt


# --------------------------------------------------------------------------- #
# ITEM 8: acceptance_rate_score é fixo — recusa de oferta não penaliza
# --------------------------------------------------------------------------- #

class TestDeclineNoEffect:
    """Recusar ou deixar expirar ofertas NÃO reduz nenhum score."""

    def test_behavior_score_same_with_zero_declines(self):
        """Walker sem nenhum decline tem acceptance_rate_score neutro (82)."""
        db = _db()
        wid = _walker_id()
        _walk(db, wid, status="Finalizado")
        result = bsvc.get_behavior_score(wid, db)
        assert result["acceptance_rate_score"] == 82.0

    def test_behavior_score_same_after_decline(self):
        """Walker com decline registrado tem o MESMO acceptance_rate_score (82)."""
        db = _db()
        wid = _walker_id()
        w = _walk(db, wid, status="Finalizado")
        _declined_attempt(db, w.id, wid)

        result = bsvc.get_behavior_score(wid, db)
        # acceptance_rate_score NÃO cai após declinar — valor base fixo.
        assert result["acceptance_rate_score"] == 82.0

    def test_behavior_score_same_after_multiple_declines(self):
        """Múltiplos declines não acumulam penalidade no acceptance_rate_score."""
        db = _db()
        wid = _walker_id()
        w1 = _walk(db, wid, status="Finalizado")
        w2 = _walk(db, wid, status="Finalizado")
        _declined_attempt(db, w1.id, wid)
        _declined_attempt(db, w2.id, wid)

        result = bsvc.get_behavior_score(wid, db)
        assert result["acceptance_rate_score"] == 82.0

    def test_behavior_score_same_after_expired_attempt(self):
        """Oferta expirada (passeador ignorou) também não penaliza o score."""
        db = _db()
        wid = _walker_id()
        w = _walk(db, wid, status="Finalizado")
        _expired_attempt(db, w.id, wid)

        result = bsvc.get_behavior_score(wid, db)
        assert result["acceptance_rate_score"] == 82.0

    def test_total_behavior_score_not_lower_after_decline(self):
        """behavior_score total não diminui após declines vs. walker sem declines."""
        db = _db()
        wid_clean = _walker_id()
        wid_declined = _walker_id()

        # Ambos têm 1 passeio finalizado
        _walk(db, wid_clean, status="Finalizado")
        w = _walk(db, wid_declined, status="Finalizado")
        # Walker com decline
        _declined_attempt(db, w.id, wid_declined)

        score_clean = bsvc.get_behavior_score(wid_clean, db)["behavior_score"]
        score_declined = bsvc.get_behavior_score(wid_declined, db)["behavior_score"]

        # Scores devem ser idênticos — decline não cria diferença
        assert score_declined == score_clean

    def test_reputation_basic_behavior_score_same_after_decline(self):
        """calculate_basic_behavior_score (reputation_service) também é imune a declines."""
        db = _db()
        wid = _walker_id()
        w = _walk(db, wid, status="Finalizado")
        _declined_attempt(db, w.id, wid)

        result = rsvc.calculate_basic_behavior_score(wid, db)
        # Valor base fixo — não calculado a partir de recusas reais
        assert result["acceptance_rate_score"] == 82.0

    def test_acceptance_rate_score_independent_of_decline_count(self):
        """Aumentar o número de declines não altera acceptance_rate_score."""
        db = _db()
        wid = _walker_id()
        walks = [_walk(db, wid, status="Finalizado") for _ in range(5)]
        scores_before = bsvc.get_behavior_score(wid, db)["acceptance_rate_score"]

        # Adiciona declines um a um — score deve permanecer igual
        for w in walks:
            _declined_attempt(db, w.id, wid)
            score_after = bsvc.get_behavior_score(wid, db)["acceptance_rate_score"]
            assert score_after == scores_before


class TestNoshowPenaltyPreserved:
    """No-show/cancelamento pós-aceite AINDA penaliza via cancellation_score.

    walk.status == "cancelado" = passeio JÁ ACEITO que não foi entregue.
    Esse é um sinal legítimo de quebra de compromisso — deve ser mantido.
    """

    def test_cancelled_walk_increases_cancellation_rate(self):
        """Walk cancelado eleva cancellation_rate e reduz cancellation_score."""
        db = _db()
        wid = _walker_id()
        _walk(db, wid, status="Finalizado")   # walk concluído
        _walk(db, wid, status="cancelado")    # walk cancelado = no-show/quebra

        result = bsvc.get_behavior_score(wid, db)
        # Cancellation rate = 1/2 = 50%, cancellation_score deve ser 50
        assert result["cancellation_score"] == 50.0
        assert result["cancellation_score"] < 100.0

    def test_no_noshow_means_perfect_cancellation_score(self):
        """Sem cancelamentos, cancellation_score é 100 (sem penalidade)."""
        db = _db()
        wid = _walker_id()
        _walk(db, wid, status="Finalizado")
        _walk(db, wid, status="Finalizado")

        result = bsvc.get_behavior_score(wid, db)
        assert result["cancellation_score"] == 100.0

    def test_decline_does_not_affect_cancellation_score(self):
        """Decline de oferta NÃO afeta cancellation_score (não é cancelamento pós-aceite)."""
        db = _db()
        wid = _walker_id()
        w = _walk(db, wid, status="Finalizado")
        _declined_attempt(db, w.id, wid)

        result = bsvc.get_behavior_score(wid, db)
        # Somente um walk finalizado, nenhum cancelado -> cancellation_score = 100
        assert result["cancellation_score"] == 100.0

    def test_cancelled_vs_declined_different_effect(self):
        """Cancelamento pós-aceite penaliza; decline de oferta não penaliza."""
        db = _db()

        # Walker A: apenas declines (não deve ser penalizado)
        wid_a = _walker_id()
        w_a = _walk(db, wid_a, status="Finalizado")
        _declined_attempt(db, w_a.id, wid_a)
        score_a = bsvc.get_behavior_score(wid_a, db)

        # Walker B: cancelamentos pós-aceite (deve ser penalizado)
        wid_b = _walker_id()
        _walk(db, wid_b, status="Finalizado")
        _walk(db, wid_b, status="cancelado")
        score_b = bsvc.get_behavior_score(wid_b, db)

        # A (com decline) deve ter cancellation_score maior que B (com cancelamento)
        assert score_a["cancellation_score"] > score_b["cancellation_score"]
        # Mas acceptance_rate_score de ambos deve ser o mesmo (fixo)
        assert score_a["acceptance_rate_score"] == score_b["acceptance_rate_score"]
