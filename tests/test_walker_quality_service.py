"""Testes de unidade para app/services/walker_quality_service.py.

Foco: list/detail de qualidade, score/risco e ordenacao.

Usa SQLite em memoria. Importa o pacote `app.models` inteiro para registrar
todos os mappers (User tem relacionamentos com Pet/Walk/Profiles), e cria todo
o schema com Base.metadata.create_all. Nao toca em producao nem em app.main.
"""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401  -> registra todos os mappers
from app.core.database import Base
from app.models.user import User
from app.models.walk import Walk
from app.models.walker_incentive import WalkerIncentive
from app.models.walker_monitoring_alert import WalkerMonitoringAlert
from app.models.walker_profile import WalkerProfile
from app.models.walker_recovery_plan import WalkerRecoveryPlan
from app.models.walker_reputation_snapshot import WalkerReputationSnapshot
from app.models.walker_review import WalkerReview
from app.models.tip_integrity_flag import TipIntegrityFlag
from app.services import walker_quality_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _user(db, uid, *, role="walker", name="Fulano de Tal"):
    user = User(id=uid, email=f"{uid}@x.com", password_hash="h", full_name=name, role=role)
    db.add(user)
    db.commit()
    return user


def _profile(db, uid, *, status="approved", name="Fulano de Tal", updated=None):
    profile = WalkerProfile(
        id=str(uuid4()),
        user_id=uid,
        full_name=name,
        status=status,
        created_at=datetime.utcnow(),
        updated_at=updated or datetime.utcnow(),
    )
    db.add(profile)
    db.commit()
    return profile


def _walk(db, walker_id, *, status="Finalizado", created=None):
    walk = Walk(
        id=str(uuid4()),
        tutor_id="tutor-x",
        walker_id=walker_id,
        pet_id="pet-x",
        scheduled_date="2026-01-01",
        duration_minutes=30,
        price=50.0,
        status=status,
        created_at=created or datetime.utcnow(),
    )
    db.add(walk)
    db.commit()
    return walk


def _review(db, walker_id, *, rating=5, flagged=False, created=None):
    review = WalkerReview(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        tutor_id="tutor-x",
        walker_id=walker_id,
        rating=rating,
        is_flagged=flagged,
        created_at=created or datetime.utcnow(),
    )
    db.add(review)
    db.commit()
    return review


def _snapshot(db, walker_id, *, hybrid=75.0, risk="normal", calculated=None):
    snap = WalkerReputationSnapshot(
        id=str(uuid4()),
        walker_id=walker_id,
        hybrid_reputation_score=hybrid,
        risk_level=risk,
        calculated_at=calculated or datetime.utcnow(),
    )
    db.add(snap)
    db.commit()
    return snap


# --------------------------------------------------------------------------
# walker_level / motivational_message (logica pura, sem DB)
# --------------------------------------------------------------------------

def test_motivational_message_paths():
    assert "primeiros passeios" in svc.motivational_message("normal", 0)
    assert "saudavel" in svc.motivational_message("normal", 3)
    assert "atencao" in svc.motivational_message("attention", 3)
    assert "sugestoes" in svc.motivational_message("risk", 3)
    assert "revisao" in svc.motivational_message("critical", 3)
    # risk_level desconhecido cai no fallback
    assert "evolucao" in svc.motivational_message("desconhecido", 3)


def test_score_breakdown_payload_shape():
    scores = {
        "rating_score": 90.0,
        "experience_score": 70.0,
        "behavior_score": 80.0,
        "consistency_score": 60.0,
        "recent_rating_score": None,
        "risk_penalty": 5.0,
        "hybrid_reputation_score": 84.0,
        "risk_level": "normal",
    }
    out = svc.score_breakdown_payload(scores)
    assert out == scores  # mesmas chaves/valores
    assert set(out.keys()) == {
        "rating_score", "experience_score", "behavior_score", "consistency_score",
        "recent_rating_score", "risk_penalty", "hybrid_reputation_score", "risk_level",
    }


def test_snapshot_payload_shape():
    db = _db()
    _user(db, "w1")
    snap = _snapshot(db, "w1", hybrid=88.0, risk="attention")
    out = svc.snapshot_payload(snap)
    assert out["hybrid_reputation_score"] == 88.0
    assert out["risk_level"] == "attention"
    assert "rating_score" in out and "risk_penalty" in out


# --------------------------------------------------------------------------
# get_quality_dashboard: list + score/risco proveniente de snapshot
# --------------------------------------------------------------------------

def test_dashboard_empty_when_no_profiles():
    db = _db()
    out = svc.get_quality_dashboard(db)
    assert out == {"items": [], "total": 0}


def test_dashboard_uses_snapshot_score_and_defaults_without_snapshot():
    db = _db()
    _user(db, "w1")
    _profile(db, "w1")
    # sem snapshot -> defaults: hybrid 75.0 / risk normal
    out = svc.get_quality_dashboard(db)
    assert out["total"] == 1
    item = out["items"][0]
    assert item["walker_id"] == "w1"
    assert item["hybrid_reputation_score"] == 75.0
    assert item["risk_level"] == "normal"

    # com snapshot -> usa o mais recente
    _snapshot(db, "w1", hybrid=42.0, risk="risk", calculated=datetime.utcnow())
    out2 = svc.get_quality_dashboard(db)
    item2 = out2["items"][0]
    assert item2["hybrid_reputation_score"] == 42.0
    assert item2["risk_level"] == "risk"


def test_dashboard_latest_snapshot_wins():
    db = _db()
    _user(db, "w1")
    _profile(db, "w1")
    _snapshot(db, "w1", hybrid=10.0, risk="critical", calculated=datetime.utcnow() - timedelta(days=2))
    _snapshot(db, "w1", hybrid=95.0, risk="normal", calculated=datetime.utcnow())
    item = svc.get_quality_dashboard(db)["items"][0]
    assert item["hybrid_reputation_score"] == 95.0
    assert item["risk_level"] == "normal"


def test_dashboard_aggregates_reviews_walks_and_counts():
    db = _db()
    _user(db, "w1")
    _profile(db, "w1", name="Maria Silva")
    # 2 avaliacoes (media 4.0), 1 passeio finalizado + 1 cancelado
    _review(db, "w1", rating=5)
    _review(db, "w1", rating=3)
    _walk(db, "w1", status="Finalizado")
    _walk(db, "w1", status="cancelado")
    # 1 alerta aberto, 1 incentivo ativo, 1 plano de recuperacao ativo, 1 flag de gorjeta aberta
    db.add(WalkerMonitoringAlert(id=str(uuid4()), walker_id="w1", alert_type="low_rating", title="t", status="open"))
    db.add(WalkerIncentive(id=str(uuid4()), walker_id="w1", incentive_type="badge", title="t", status="active"))
    db.add(WalkerRecoveryPlan(id=str(uuid4()), walker_id="w1", status="active"))
    db.add(TipIntegrityFlag(id=str(uuid4()), walker_id="w1", flag_type="x", status="open"))
    db.commit()

    item = svc.get_quality_dashboard(db)["items"][0]
    assert item["name"] == "Maria Silva"
    assert item["reviews_count"] == 2
    assert item["rating_average"] == 4.0
    assert item["total_walks"] == 1  # so finalizados contam como total_walks
    assert item["open_alerts_count"] == 1
    assert item["active_incentives_count"] == 1
    assert item["active_recovery_plan"] is True
    assert item["tip_flags_count"] == 1
    # 1 cancelado de 2 passeios totais => 50%
    assert item["cancellation_rate"] == 50.0


def test_dashboard_default_name_when_profile_full_name_empty():
    db = _db()
    _user(db, "w1")
    _profile(db, "w1", name="")
    item = svc.get_quality_dashboard(db)["items"][0]
    assert item["name"] == "Passeador Aumigao"


def test_dashboard_status_filter():
    db = _db()
    _user(db, "w1")
    _user(db, "w2")
    _profile(db, "w1", status="approved")
    _profile(db, "w2", status="suspended")
    out = svc.get_quality_dashboard(db, status="approved")
    ids = {i["walker_id"] for i in out["items"]}
    assert ids == {"w1"}
    # status="all" nao filtra
    out_all = svc.get_quality_dashboard(db, status="all")
    assert {i["walker_id"] for i in out_all["items"]} == {"w1", "w2"}


def test_dashboard_risk_level_filter():
    db = _db()
    _user(db, "w1")
    _user(db, "w2")
    _profile(db, "w1")
    _profile(db, "w2")
    _snapshot(db, "w1", risk="risk")
    _snapshot(db, "w2", risk="normal")
    out = svc.get_quality_dashboard(db, risk_level="risk")
    assert {i["walker_id"] for i in out["items"]} == {"w1"}


def test_dashboard_boolean_filters():
    db = _db()
    _user(db, "w1")
    _user(db, "w2")
    _profile(db, "w1")
    _profile(db, "w2")
    # w1 tem alerta aberto, plano ativo e flag; w2 nao tem nada
    db.add(WalkerMonitoringAlert(id=str(uuid4()), walker_id="w1", alert_type="x", title="t", status="open"))
    db.add(WalkerRecoveryPlan(id=str(uuid4()), walker_id="w1", status="active"))
    db.add(TipIntegrityFlag(id=str(uuid4()), walker_id="w1", flag_type="x", status="open"))
    db.commit()

    assert {i["walker_id"] for i in svc.get_quality_dashboard(db, has_open_alerts=True)["items"]} == {"w1"}
    assert {i["walker_id"] for i in svc.get_quality_dashboard(db, has_open_alerts=False)["items"]} == {"w2"}
    assert {i["walker_id"] for i in svc.get_quality_dashboard(db, has_recovery_plan=True)["items"]} == {"w1"}
    assert {i["walker_id"] for i in svc.get_quality_dashboard(db, has_recovery_plan=False)["items"]} == {"w2"}
    assert {i["walker_id"] for i in svc.get_quality_dashboard(db, has_tip_flags=True)["items"]} == {"w1"}
    assert {i["walker_id"] for i in svc.get_quality_dashboard(db, has_tip_flags=False)["items"]} == {"w2"}


def test_dashboard_limit_caps_profiles():
    db = _db()
    for i in range(3):
        _user(db, f"w{i}")
        _profile(db, f"w{i}")
    out = svc.get_quality_dashboard(db, limit=2)
    assert out["total"] == 2


# --------------------------------------------------------------------------
# Ordenacao
# --------------------------------------------------------------------------

def test_dashboard_orders_non_normal_risk_first():
    db = _db()
    _user(db, "normal1")
    _user(db, "risk1")
    _profile(db, "normal1")
    _profile(db, "risk1")
    _snapshot(db, "normal1", risk="normal", hybrid=90.0)
    _snapshot(db, "risk1", risk="risk", hybrid=50.0)
    items = svc.get_quality_dashboard(db)["items"]
    # risco diferente de normal vem primeiro
    assert items[0]["walker_id"] == "risk1"
    assert items[1]["walker_id"] == "normal1"


def test_dashboard_orders_more_open_alerts_first_within_same_risk():
    db = _db()
    _user(db, "few")
    _user(db, "many")
    _profile(db, "few")
    _profile(db, "many")
    _snapshot(db, "few", risk="risk", hybrid=80.0)
    _snapshot(db, "many", risk="risk", hybrid=80.0)
    db.add(WalkerMonitoringAlert(id=str(uuid4()), walker_id="few", alert_type="a", title="t", status="open"))
    db.add(WalkerMonitoringAlert(id=str(uuid4()), walker_id="many", alert_type="a", title="t", status="open"))
    db.add(WalkerMonitoringAlert(id=str(uuid4()), walker_id="many", alert_type="b", title="t", status="open"))
    db.commit()
    items = svc.get_quality_dashboard(db)["items"]
    assert items[0]["walker_id"] == "many"  # mais alertas abertos primeiro
    assert items[1]["walker_id"] == "few"


def test_dashboard_score_tiebreak_documents_current_behavior():
    """Desempate por score: mesmo risco e mesmo numero de alertas.

    Comportamento ATUAL (documentado): a chave usa `-hybrid_reputation_score`
    junto com sort(reverse=True). O `-` combinado com reverse=True faz o
    MENOR score aparecer primeiro como desempate (provavel inversao nao
    intencional; ver bug_or_gap). Este teste fixa o comportamento real.
    """
    db = _db()
    _user(db, "high")
    _user(db, "low")
    _profile(db, "high")
    _profile(db, "low")
    _snapshot(db, "high", risk="risk", hybrid=90.0)
    _snapshot(db, "low", risk="risk", hybrid=10.0)
    items = svc.get_quality_dashboard(db)["items"]
    # comportamento atual: menor score primeiro no desempate
    assert items[0]["walker_id"] == "low"
    assert items[1]["walker_id"] == "high"


# --------------------------------------------------------------------------
# get_walker_quality_detail
# --------------------------------------------------------------------------

def test_detail_not_found_raises_404():
    db = _db()
    with pytest.raises(HTTPException) as exc:
        svc.get_walker_quality_detail("inexistente", db)
    assert exc.value.status_code == 404


def test_detail_returns_full_structure():
    db = _db()
    _user(db, "w1", name="Joao Souza")
    _profile(db, "w1", status="approved", name="Joao Souza")
    _review(db, "w1", rating=5)
    _walk(db, "w1", status="Finalizado")
    detail = svc.get_walker_quality_detail("w1", db)

    assert set(detail.keys()) >= {
        "walker", "score_breakdown", "snapshots", "reviews", "alerts",
        "recovery_plan", "incentives", "tip_integrity_flags", "recommendations", "tip_policy",
    }
    assert detail["walker"]["walker_id"] == "w1"
    assert detail["walker"]["name"] == "Joao Souza"
    # create_reputation_snapshot foi chamado -> pelo menos 1 snapshot
    assert len(detail["snapshots"]) >= 1
    assert len(detail["reviews"]) == 1
    assert detail["reviews"][0]["rating"] == 5
    assert detail["tip_policy"] == svc.TIP_REPUTATION_POLICY
    assert isinstance(detail["recommendations"], list) and detail["recommendations"]


def test_detail_risk_walker_gets_recovery_plan_created():
    db = _db()
    _user(db, "w1")
    _profile(db, "w1", status="approved")
    # 3 avaliacoes ruins -> rating < 4.0 -> risk_level critical
    _review(db, "w1", rating=2)
    _review(db, "w1", rating=2)
    _review(db, "w1", rating=2)
    detail = svc.get_walker_quality_detail("w1", db)
    assert detail["walker"]["risk_level"] in {"critical", "risk"}
    # get_or_create_recovery_plan cria plano para risco critical/risk/attention
    assert detail["recovery_plan"] is not None
    assert detail["recovery_plan"]["status"] == "active"


# --------------------------------------------------------------------------
# walker_quality_item (computa via servicos de reputacao, nao snapshot)
# --------------------------------------------------------------------------

def test_walker_quality_item_no_history_defaults():
    db = _db()
    _user(db, "w1")
    profile = _profile(db, "w1", status="approved")
    item = svc.walker_quality_item(profile, db)
    assert item["walker_id"] == "w1"
    assert item["reviews_count"] == 0
    assert item["total_walks"] == 0
    assert item["level"] == "Bronze"
    # sem reviews/walks, risco normal e cancelamento 0
    assert item["risk_level"] == "normal"
    assert item["cancellation_rate"] == 0.0
