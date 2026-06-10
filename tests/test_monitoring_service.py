"""Testes de unidade para app/services/monitoring_service.py.

Foco: geracao de alertas automaticos em evaluate_monitoring_alerts
(low_rating / high_cancellation / negative_reviews), seus thresholds e
severidades, alem de deduplicacao (create_alert), open_alerts e update_alert.

DB: SQLite em memoria (apenas tabelas walks + walker_reviews +
walker_monitoring_alerts). Nao importa app.main, nao usa banco real.

Comportamento real verificado no codigo:
- low_rating: reviews_count >= 3 e rating_average < 4.3
    severity = "high" se rating_average < 4.0 senao "medium"
- high_cancellation: behavior["cancellation_rate"] (em %) >= 12
    severity = "high" se >= 25 senao "medium"
- negative_reviews: numero de reviews com rating <= 3 entre as 6 mais
    recentes >= 2  ->  severity "medium" (fixo)
"""
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_review import WalkerReview
from app.services import monitoring_service as svc

WALKER_ID = "walker-1"


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Walk.__table__,
            WalkerReview.__table__,
            WalkerMonitoringAlert.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


_review_counter = 0


def _add_review(db, *, rating, walker_id=WALKER_ID, created_at=None, when_offset=None):
    """Cria um WalkerReview. created_at controla a ordem (desc) usada em
    evaluate_monitoring_alerts ao pegar as 6 mais recentes."""
    global _review_counter
    _review_counter += 1
    if created_at is None:
        # offset crescente garante ordem deterministica se nao informado
        base = datetime(2026, 1, 1, 12, 0, 0)
        created_at = base + timedelta(minutes=_review_counter)
    if when_offset is not None:
        created_at = datetime(2026, 1, 1, 12, 0, 0) + timedelta(minutes=when_offset)
    review = WalkerReview(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        tutor_id="tutor-x",
        walker_id=walker_id,
        rating=rating,
        created_at=created_at,
    )
    db.add(review)
    db.commit()
    return review


def _add_walk(db, *, status, walker_id=WALKER_ID):
    walk = Walk(
        id=str(uuid4()),
        tutor_id="tutor-x",
        walker_id=walker_id,
        pet_id="pet-x",
        scheduled_date="2026-01-01",
        duration_minutes=30,
        price=50.0,
        status=status,
        created_at=datetime(2026, 1, 1, 10, 0, 0),
    )
    db.add(walk)
    db.commit()
    return walk


def _types(alerts):
    return {a.alert_type for a in alerts}


def _by_type(alerts, alert_type):
    matches = [a for a in alerts if a.alert_type == alert_type]
    return matches[0] if matches else None


# ----------------------------------------------------------------------------
# Caminho "limpo": nenhum alerta
# ----------------------------------------------------------------------------

def test_no_alerts_when_no_data():
    db = _db()
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    assert result == []


def test_no_low_rating_below_three_reviews():
    db = _db()
    # 2 reviews ruins -> reviews_count < 3, nao dispara low_rating.
    _add_review(db, rating=1)
    _add_review(db, rating=1)
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    assert "low_rating" not in _types(result)


def test_no_low_rating_when_rating_at_or_above_threshold():
    db = _db()
    # 3 reviews com media exatamente 4.3 (4.3 < 4.3 e' falso).
    _add_review(db, rating=5)
    _add_review(db, rating=4)
    _add_review(db, rating=4)
    # media = 13/3 = 4.33 -> >= 4.3, nao dispara
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    assert "low_rating" not in _types(result)


# ----------------------------------------------------------------------------
# low_rating: thresholds e severidades
# ----------------------------------------------------------------------------

def test_low_rating_medium_severity():
    db = _db()
    # media em [4.0, 4.3): severidade "medium".
    # 4 + 4 + 4 = 12/3 = 4.0  -> 4.0 < 4.3 verdadeiro, 4.0 < 4.0 falso => medium
    _add_review(db, rating=4)
    _add_review(db, rating=4)
    _add_review(db, rating=4)
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    alert = _by_type(result, "low_rating")
    assert alert is not None
    assert alert.severity == "medium"
    assert alert.source == "reputation"
    assert alert.status == "open"


def test_low_rating_high_severity():
    db = _db()
    # media < 4.0 -> severidade "high".
    # 4 + 4 + 3 = 11/3 = 3.67 < 4.0
    _add_review(db, rating=4)
    _add_review(db, rating=4)
    _add_review(db, rating=3)
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    alert = _by_type(result, "low_rating")
    assert alert is not None
    assert alert.severity == "high"


# ----------------------------------------------------------------------------
# high_cancellation: thresholds e severidades
# (cancellation_rate vem em PORCENTAGEM de calculate_basic_behavior_score)
# ----------------------------------------------------------------------------

def test_no_high_cancellation_below_threshold():
    db = _db()
    # 1 cancelado em 10 walks = 10% < 12% -> nao dispara.
    for _ in range(9):
        _add_walk(db, status="Finalizado")
    _add_walk(db, status="cancelado")
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    assert "high_cancellation" not in _types(result)


def test_high_cancellation_medium_severity():
    db = _db()
    # cancelamento em [12, 25): "medium".
    # 2 cancelados em 10 = 20% -> >= 12 e < 25 => medium
    for _ in range(8):
        _add_walk(db, status="Finalizado")
    _add_walk(db, status="cancelado")
    _add_walk(db, status="cancelado")
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    alert = _by_type(result, "high_cancellation")
    assert alert is not None
    assert alert.severity == "medium"
    assert alert.source == "system"


def test_high_cancellation_high_severity():
    db = _db()
    # cancelamento >= 25%: "high".
    # 3 cancelados em 10 = 30% -> high
    for _ in range(7):
        _add_walk(db, status="Finalizado")
    for _ in range(3):
        _add_walk(db, status="cancelado")
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    alert = _by_type(result, "high_cancellation")
    assert alert is not None
    assert alert.severity == "high"


def test_high_cancellation_exactly_25_is_high():
    db = _db()
    # 1 cancelado em 4 = 25% -> "high" (>= 25).
    for _ in range(3):
        _add_walk(db, status="Finalizado")
    _add_walk(db, status="cancelado")
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    alert = _by_type(result, "high_cancellation")
    assert alert is not None
    assert alert.severity == "high"


# ----------------------------------------------------------------------------
# negative_reviews: contagem entre as 6 mais recentes, severidade fixa "medium"
# ----------------------------------------------------------------------------

def test_negative_reviews_alert_medium():
    db = _db()
    # 2 reviews com rating <= 3 entre as ultimas 6 -> dispara, severity medium.
    _add_review(db, rating=2)
    _add_review(db, rating=3)
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    alert = _by_type(result, "negative_reviews")
    assert alert is not None
    assert alert.severity == "medium"
    assert alert.source == "review"


def test_negative_reviews_not_triggered_with_single_negative():
    db = _db()
    # apenas 1 review negativa -> < 2, nao dispara negative_reviews.
    _add_review(db, rating=2)
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    assert "negative_reviews" not in _types(result)


def test_negative_reviews_only_counts_latest_six():
    db = _db()
    # As 2 reviews negativas sao ANTIGAS; as 6 mais recentes sao todas 5.
    # walker_reviews_query ordena por created_at desc e limita a 6.
    # Antigas (rating 3) com created_at menor.
    _add_review(db, rating=3, when_offset=1)
    _add_review(db, rating=3, when_offset=2)
    # 6 recentes positivas (offsets maiores)
    for i in range(6):
        _add_review(db, rating=5, when_offset=10 + i)
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    # As negativas ficaram fora da janela das 6 mais recentes.
    assert "negative_reviews" not in _types(result)


def test_negative_reviews_rating_three_counts_as_negative():
    db = _db()
    # rating == 3 conta como negativo (rating <= 3).
    _add_review(db, rating=3)
    _add_review(db, rating=3)
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    assert "negative_reviews" in _types(result)


# ----------------------------------------------------------------------------
# Combinacao: varios alertas ao mesmo tempo
# ----------------------------------------------------------------------------

def test_multiple_alerts_together():
    db = _db()
    # 3 reviews ruins (media baixa + negativos) e cancelamentos altos.
    _add_review(db, rating=2)
    _add_review(db, rating=3)
    _add_review(db, rating=3)
    # walks: 3 cancelados em 4 = 75% cancelamento
    for _ in range(1):
        _add_walk(db, status="Finalizado")
    for _ in range(3):
        _add_walk(db, status="cancelado")
    result = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    types = _types(result)
    assert "low_rating" in types
    assert "high_cancellation" in types
    assert "negative_reviews" in types
    assert _by_type(result, "low_rating").severity == "high"  # media < 4.0
    assert _by_type(result, "high_cancellation").severity == "high"  # 75% >= 25


# ----------------------------------------------------------------------------
# Deduplicacao (create_alert): nao duplica alerta aberto do mesmo tipo
# ----------------------------------------------------------------------------

def test_evaluate_is_idempotent_for_open_alerts():
    db = _db()
    _add_review(db, rating=2)
    _add_review(db, rating=3)
    _add_review(db, rating=3)
    svc.evaluate_monitoring_alerts(WALKER_ID, db)
    svc.evaluate_monitoring_alerts(WALKER_ID, db)
    # Mesmo apos duas avaliacoes, ha apenas 1 alerta por tipo (status open).
    low = db.query(WalkerMonitoringAlert).filter(
        WalkerMonitoringAlert.walker_id == WALKER_ID,
        WalkerMonitoringAlert.alert_type == "low_rating",
    ).count()
    assert low == 1


def test_resolved_alert_does_not_block_new_one():
    db = _db()
    _add_review(db, rating=2)
    _add_review(db, rating=3)
    _add_review(db, rating=3)
    first = svc.evaluate_monitoring_alerts(WALKER_ID, db)
    low_alert = _by_type(first, "low_rating")
    # Resolve o alerta existente.
    svc.update_alert(low_alert.id, "resolved", None, "admin-1", db)
    # Nova avaliacao deve criar um novo low_rating (o anterior nao esta mais open).
    svc.evaluate_monitoring_alerts(WALKER_ID, db)
    low_total = db.query(WalkerMonitoringAlert).filter(
        WalkerMonitoringAlert.walker_id == WALKER_ID,
        WalkerMonitoringAlert.alert_type == "low_rating",
    ).count()
    assert low_total == 2


# ----------------------------------------------------------------------------
# create_alert direto
# ----------------------------------------------------------------------------

def test_create_alert_returns_existing_when_open():
    db = _db()
    a1 = svc.create_alert(WALKER_ID, "low_rating", "high", "t", "d", "reputation", db)
    a2 = svc.create_alert(WALKER_ID, "low_rating", "medium", "t2", "d2", "reputation", db)
    assert a1.id == a2.id
    # mantem os dados do primeiro (nao atualiza severidade)
    assert a2.severity == "high"


def test_create_alert_in_review_status_also_dedupes():
    db = _db()
    a1 = svc.create_alert(WALKER_ID, "low_rating", "high", "t", "d", "reputation", db)
    a1.status = "in_review"
    db.commit()
    a2 = svc.create_alert(WALKER_ID, "low_rating", "high", "t", "d", "reputation", db)
    assert a1.id == a2.id


# ----------------------------------------------------------------------------
# open_alerts
# ----------------------------------------------------------------------------

def test_open_alerts_filters_status():
    db = _db()
    open_a = svc.create_alert(WALKER_ID, "low_rating", "high", "t", "d", "reputation", db)
    resolved = svc.create_alert(WALKER_ID, "high_cancellation", "high", "t", "d", "system", db)
    svc.update_alert(resolved.id, "resolved", None, "admin", db)
    result = svc.open_alerts(WALKER_ID, db)
    ids = {a.id for a in result}
    assert open_a.id in ids
    assert resolved.id not in ids


# ----------------------------------------------------------------------------
# update_alert
# ----------------------------------------------------------------------------

def test_update_alert_resolved_sets_resolved_at():
    db = _db()
    alert = svc.create_alert(WALKER_ID, "low_rating", "high", "t", "d", "reputation", db)
    updated = svc.update_alert(alert.id, "resolved", "tudo certo", "admin-9", db)
    assert updated.status == "resolved"
    assert updated.resolved_at is not None
    assert updated.admin_notes == "tudo certo"
    assert updated.reviewed_by_admin_id == "admin-9"


def test_update_alert_in_review_does_not_set_resolved_at():
    db = _db()
    alert = svc.create_alert(WALKER_ID, "low_rating", "high", "t", "d", "reputation", db)
    updated = svc.update_alert(alert.id, "in_review", None, "admin-9", db)
    assert updated.status == "in_review"
    assert updated.resolved_at is None


def test_update_alert_keeps_previous_notes_when_none():
    db = _db()
    alert = svc.create_alert(WALKER_ID, "low_rating", "high", "t", "d", "reputation", db)
    svc.update_alert(alert.id, "in_review", "primeira nota", "admin-1", db)
    updated = svc.update_alert(alert.id, "resolved", None, "admin-2", db)
    # admin_notes = None or alert.admin_notes -> mantem a anterior
    assert updated.admin_notes == "primeira nota"


def test_update_alert_not_found_raises_404():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        svc.update_alert("inexistente", "resolved", None, "admin", db)
    assert exc.value.status_code == 404
