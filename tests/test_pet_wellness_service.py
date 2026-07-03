"""Testes do serviço de Índice de Bem-estar (Perfil Vivo 2.0, Fase B).

O score é 100% runtime (sem persistência, sem migration). Os testes cobrem os
3 componentes (clínico/rotina/comportamento), a composição ponderada, os rótulos
por faixa e a tendência 30d (recompute com cutoff `as_of`).
"""
from __future__ import annotations

import app.models  # noqa: F401

from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.pet import Pet
from app.models.pet_health_record import PetHealthRecord
from app.models.tenant import Tenant
from app.models.user import User
from app.models.walk import Walk
from app.models.walk_observation import WalkObservation
from app.services import pet_wellness_service as wellness


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(Tenant(id="t1", name="T1", slug="t1", status="active", plan="pro"))
    db.add(User(id="u1", email="u1@x.com", password_hash="x", role="tutor", tenant_id="t1"))
    db.add(User(id="wk", email="wk@x.com", password_hash="x", role="walker", tenant_id="t1"))
    db.add(Pet(id="p1", tutor_id="u1", tenant_id="t1", name="Rex"))
    db.commit()
    return db


def _add_record(db, kind, *, valid_days_from_now, applied_days_ago=1):
    """Adiciona um registro de carteira com validade relativa a hoje."""
    today = date.today()
    valid_until = today + timedelta(days=valid_days_from_now) if valid_days_from_now is not None else None
    db.add(PetHealthRecord(
        pet_id="p1", tenant_id="t1", kind=kind, name=f"{kind}-rec",
        applied_at=today - timedelta(days=applied_days_ago),
        valid_until=valid_until, created_by_role="tutor",
    ))
    db.commit()


def _add_walk(db, *, days_ago, status="completed", wid=None):
    now = datetime.utcnow()
    db.add(Walk(
        id=wid or f"w{days_ago}-{status}-{now.timestamp()}",
        tutor_id="u1", pet_id="p1", tenant_id="t1",
        scheduled_date="2026-07-01", duration_minutes=30, price=0.0,
        status=status, created_at=now - timedelta(days=days_ago),
    ))
    db.commit()


def _add_obs(db, *, days_ago, incident=False, socialization=None, mood=None, wid=None):
    now = datetime.utcnow()
    db.add(WalkObservation(
        walk_id=wid or f"ow{days_ago}-{now.timestamp()}",
        pet_id="p1", tenant_id="t1", walker_user_id="wk",
        incident=incident, socialization=socialization, mood=mood,
        created_at=now - timedelta(days=days_ago),
    ))
    db.commit()


# ---------------------------------------------------------------------------
# Componente clínico
# ---------------------------------------------------------------------------

def test_clinical_vaccines_em_dia_high():
    db = _db()
    # Só vacina em dia (vermífugo/antipulgas não trilhados = neutro, não punem).
    _add_record(db, "vaccine", valid_days_from_now=200)
    comp = wellness.compute_clinical(db, "p1")
    assert comp["score"] >= 80
    assert "em dia" in comp["detail"]


def test_clinical_all_kinds_em_dia_full():
    db = _db()
    _add_record(db, "vaccine", valid_days_from_now=200)
    _add_record(db, "dewormer", valid_days_from_now=200)
    _add_record(db, "flea_tick", valid_days_from_now=200)
    comp = wellness.compute_clinical(db, "p1")
    assert comp["score"] == 100


def test_clinical_vencendo_partial():
    db = _db()
    _add_record(db, "vaccine", valid_days_from_now=10)  # vencendo (<=30d)
    comp = wellness.compute_clinical(db, "p1")
    assert 0 < comp["score"] < 100
    assert "vencendo" in comp["detail"]


def test_clinical_atrasada_lower_than_em_dia():
    db = _db()
    _add_record(db, "vaccine", valid_days_from_now=-5)  # atrasada
    atrasada = wellness.compute_clinical(db, "p1")["score"]
    assert "atrasada" in wellness.compute_clinical(db, "p1")["detail"]

    db2 = _db()
    _add_record(db2, "vaccine", valid_days_from_now=200)  # em dia
    em_dia = wellness.compute_clinical(db2, "p1")["score"]
    # Vacina atrasada derruba bem o componente clínico (vacina é o kind dominante).
    assert atrasada < em_dia
    assert atrasada <= 45


def test_clinical_sem_registro_low_but_not_zero():
    db = _db()
    comp = wellness.compute_clinical(db, "p1")
    # Sem nenhum registro: vacina ausente domina → baixo, com detalhe acionável.
    assert comp["score"] <= 45
    assert "sem registro" in comp["detail"]


def test_clinical_vaccine_weighs_more_than_dewormer():
    """Vacina atrasada derruba mais que vermífugo atrasado (peso maior)."""
    db_a = _db()
    _add_record(db_a, "vaccine", valid_days_from_now=-5)
    _add_record(db_a, "dewormer", valid_days_from_now=200)
    score_vaccine_bad = wellness.compute_clinical(db_a, "p1")["score"]

    db_b = _db()
    _add_record(db_b, "vaccine", valid_days_from_now=200)
    _add_record(db_b, "dewormer", valid_days_from_now=-5)
    score_dewormer_bad = wellness.compute_clinical(db_b, "p1")["score"]

    assert score_vaccine_bad < score_dewormer_bad


# ---------------------------------------------------------------------------
# Componente rotina (passeios concluídos 30d)
# ---------------------------------------------------------------------------

def test_routine_zero_walks():
    db = _db()
    comp = wellness.compute_routine(db, "p1")
    assert comp["score"] == 0
    assert "0 passeios" in comp["detail"]


def test_routine_scale_buckets():
    db = _db()
    # 5 passeios concluídos nos últimos 30d → bucket 4-7 = 70
    for i in range(5):
        _add_walk(db, days_ago=i + 1, status="completed", wid=f"w{i}")
    comp = wellness.compute_routine(db, "p1")
    assert comp["score"] == 70


def test_routine_only_completed_counted():
    db = _db()
    _add_walk(db, days_ago=1, status="completed", wid="wc")
    _add_walk(db, days_ago=1, status="Agendado", wid="wa")
    comp = wellness.compute_routine(db, "p1")
    # 1 concluído → bucket 1-3 = 40
    assert comp["score"] == 40


def test_routine_excludes_old_walks():
    db = _db()
    _add_walk(db, days_ago=40, status="completed", wid="wold")  # fora da janela 30d
    comp = wellness.compute_routine(db, "p1")
    assert comp["score"] == 0


def test_routine_twelve_plus_full():
    db = _db()
    for i in range(13):
        _add_walk(db, days_ago=1, status="completed", wid=f"w{i}")
    comp = wellness.compute_routine(db, "p1")
    assert comp["score"] == 100


# ---------------------------------------------------------------------------
# Componente comportamento (observações 90d)
# ---------------------------------------------------------------------------

def test_behavior_no_observations_neutral():
    db = _db()
    comp = wellness.compute_behavior(db, "p1")
    assert comp["score"] == 70
    assert "sem dados" in comp["detail"].lower()


def test_behavior_clean_walks_high():
    db = _db()
    for i in range(4):
        _add_obs(db, days_ago=i + 1, incident=False, socialization="good", mood="calm", wid=f"o{i}")
    comp = wellness.compute_behavior(db, "p1")
    assert comp["score"] >= 90


def test_behavior_incidents_penalize():
    db = _db()
    for i in range(2):
        _add_obs(db, days_ago=i + 1, incident=True, wid=f"oi{i}")
    for i in range(2):
        _add_obs(db, days_ago=i + 10, incident=False, wid=f"on{i}")
    comp = wellness.compute_behavior(db, "p1")
    # 50% de passeios com incidente → penalidade forte
    assert comp["score"] < 70
    assert "incidente" in comp["detail"].lower()


def test_behavior_excludes_old_observations():
    db = _db()
    _add_obs(db, days_ago=100, incident=True, wid="oold")
    comp = wellness.compute_behavior(db, "p1")
    # Fora dos 90d → tratado como sem dados
    assert comp["score"] == 70


# ---------------------------------------------------------------------------
# Composição e rótulos
# ---------------------------------------------------------------------------

def test_compute_wellness_shape_and_weights():
    db = _db()
    _add_record(db, "vaccine", valid_days_from_now=200)
    for i in range(5):
        _add_walk(db, days_ago=i + 1, status="completed", wid=f"w{i}")
    payload = wellness.compute_wellness(db, "p1")

    assert payload["pet_id"] == "p1"
    assert isinstance(payload["score"], int)
    assert 0 <= payload["score"] <= 100
    assert payload["label"] in {"Ótimo", "Bom", "Atenção", "Alerta"}
    keys = {c["key"] for c in payload["components"]}
    assert keys == {"clinico", "rotina", "comportamento"}
    weights = {c["key"]: c["weight"] for c in payload["components"]}
    assert weights == {"clinico": 40, "rotina": 35, "comportamento": 25}
    for c in payload["components"]:
        assert c["detail"], "detail deve sempre explicar o porquê"
    assert "trend" in payload
    assert payload["trend"]["direction"] in {"up", "down", "stable"}
    assert "computed_at" in payload


def test_labels_by_band():
    assert wellness.score_label(85) == "Ótimo"
    assert wellness.score_label(80) == "Ótimo"
    assert wellness.score_label(70) == "Bom"
    assert wellness.score_label(60) == "Bom"
    assert wellness.score_label(50) == "Atenção"
    assert wellness.score_label(40) == "Atenção"
    assert wellness.score_label(30) == "Alerta"


def test_weighted_score_matches_manual():
    db = _db()
    _add_record(db, "vaccine", valid_days_from_now=200)
    for i in range(5):
        _add_walk(db, days_ago=i + 1, status="completed", wid=f"w{i}")  # rotina=70
    # comportamento sem obs = 70
    payload = wellness.compute_wellness(db, "p1")
    by_key = {c["key"]: c["score"] for c in payload["components"]}
    expected = round((by_key["clinico"] * 40 + by_key["rotina"] * 35 + by_key["comportamento"] * 25) / 100)
    assert payload["score"] == expected
    assert by_key["rotina"] == 70
    assert by_key["comportamento"] == 70


# ---------------------------------------------------------------------------
# Tendência 30d (recompute com as_of)
# ---------------------------------------------------------------------------

def test_trend_up_when_routine_improves():
    db = _db()
    # Passeios só na janela recente (últimos 30d) → 30d atrás a rotina era 0.
    for i in range(5):
        _add_walk(db, days_ago=i + 1, status="completed", wid=f"w{i}")
    payload = wellness.compute_wellness(db, "p1")
    assert payload["trend"]["direction"] == "up"
    assert payload["trend"]["delta"] > 0
    assert payload["trend"]["window_days"] == 30


def test_trend_stable_when_no_change():
    db = _db()
    _add_record(db, "vaccine", valid_days_from_now=400)  # em dia nos dois cortes
    payload = wellness.compute_wellness(db, "p1")
    # Sem passeios/obs em nenhum corte → mesma pontuação → stable.
    assert payload["trend"]["direction"] == "stable"
    assert abs(payload["trend"]["delta"]) <= 5
