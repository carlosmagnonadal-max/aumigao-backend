"""Testes de unidade do walker_operational_score_service.

Cobre: estrutura sem walker, score base + ajustes (passeios, rating,
eventos operacionais, rejeicoes de finalizacao) e os cortes do
reliability_label. Usa SQLite em memoria, sem app.main / banco real.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.walk import Walk
from app.models.walk_completion_review import WalkCompletionReview
from app.models.walk_operational_event import WalkOperationalEvent
from app.models.walk_review import WalkReview
from app.services import walker_operational_score_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Walk.__table__,
            WalkReview.__table__,
            WalkOperationalEvent.__table__,
            WalkCompletionReview.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _walk(db, walker_id, *, finalizado=True):
    """Cria um passeio que conta como concluido para o walker.

    Usa status 'Finalizado' (uma das duas condicoes do _completed_walks).
    """
    walk = Walk(
        id=str(uuid4()),
        tutor_id="tutor1",
        walker_id=walker_id,
        pet_id="pet1",
        scheduled_date="2026-06-01",
        duration_minutes=30,
        price=50.0,
        status="Finalizado" if finalizado else "Agendado",
        operational_status="ride_completed" if finalizado else "ride_scheduled",
    )
    db.add(walk)
    db.commit()
    return walk


def _review(db, walker_id, rating):
    rv = WalkReview(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        tutor_id="tutor1",
        walker_id=walker_id,
        rating=rating,
    )
    db.add(rv)
    db.commit()
    return rv


def _event(db, walker_id, event_type, *, severity="low", days_ago=1):
    ev = WalkOperationalEvent(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        walker_id=walker_id,
        event_type=event_type,
        severity=severity,
        created_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    db.add(ev)
    db.commit()
    return ev


def _rejection(db, walker_id, status="rejected"):
    cr = WalkCompletionReview(
        id=str(uuid4()),
        walk_id=str(uuid4()),
        walker_user_id=walker_id,
        tutor_user_id="tutor1",
        status=status,
    )
    db.add(cr)
    db.commit()
    return cr


# ---------------------------------------------------------------------------
# walker_id ausente
# ---------------------------------------------------------------------------

def test_sem_walker_id_retorna_score_em_formacao():
    db = _db()
    result = svc.calculate_walker_operational_score(None, db)
    assert result["operational_score"] == 0
    assert result["reliability_label"] == "Em formação"
    assert result["score_details"]["completed_walks"] == 0
    assert result["score_factors"]["positivos"] == []
    assert result["score_policy"].startswith("Indicador informativo")


def test_walker_id_vazio_tratado_como_ausente():
    db = _db()
    result = svc.calculate_walker_operational_score("", db)
    assert result["operational_score"] == 0
    assert result["reliability_label"] == "Em formação"


# ---------------------------------------------------------------------------
# walker sem historico nenhum
# ---------------------------------------------------------------------------

def test_walker_sem_historico_score_base_70_em_formacao():
    db = _db()
    result = svc.calculate_walker_operational_score("w1", db)
    # Nenhum passeio: score permanece base 70 (sem ajustes).
    assert result["operational_score"] == 70
    # completed_count < 3 -> "Em formação"
    assert result["reliability_label"] == "Em formação"
    assert result["score_details"] == {
        "completed_walks": 0,
        "rating_avg": 0,
        "rating_count": 0,
        "recent_operational_events": 0,
        "high_attention_events": 0,
        "completion_rejections": 0,
    }
    # Sem positivos reais -> mensagem de formacao
    assert result["score_factors"]["positivos"] == [
        "Score em formação após os primeiros passeios validados."
    ]
    assert result["score_factors"]["pontos_de_atencao"] == [
        "Sem pontos críticos recentes registrados."
    ]


# ---------------------------------------------------------------------------
# ajuste por passeios concluidos
# ---------------------------------------------------------------------------

def test_passeios_concluidos_somam_ate_o_teto_de_12():
    db = _db()
    for _ in range(3):
        _walk(db, "w1")
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + min(12, 3*2)=6 -> 76
    assert result["operational_score"] == 76
    assert result["score_details"]["completed_walks"] == 3
    assert "3 passeio(s) concluído(s) com validação operacional." in result["score_factors"]["positivos"]


def test_bonus_de_passeios_limitado_a_12_e_bonus_consistencia():
    db = _db()
    for _ in range(10):
        _walk(db, "w1")
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + min(12, 20)=12 + (completed>=10 ? +5) = 87
    assert result["operational_score"] == 87
    assert result["score_details"]["completed_walks"] == 10
    assert "Histórico operacional consistente no beta." in result["score_factors"]["positivos"]


def test_completed_via_operational_status_ride_completed():
    db = _db()
    # status != Finalizado mas operational_status == ride_completed
    walk = Walk(
        id=str(uuid4()),
        tutor_id="tutor1",
        walker_id="w1",
        pet_id="pet1",
        scheduled_date="2026-06-01",
        duration_minutes=30,
        price=50.0,
        status="Agendado",
        operational_status="ride_completed",
    )
    db.add(walk)
    db.commit()
    result = svc.calculate_walker_operational_score("w1", db)
    assert result["score_details"]["completed_walks"] == 1


def test_completed_conta_assigned_walker_id():
    db = _db()
    walk = Walk(
        id=str(uuid4()),
        tutor_id="tutor1",
        walker_id=None,
        assigned_walker_id="w1",
        pet_id="pet1",
        scheduled_date="2026-06-01",
        duration_minutes=30,
        price=50.0,
        status="Finalizado",
    )
    db.add(walk)
    db.commit()
    result = svc.calculate_walker_operational_score("w1", db)
    assert result["score_details"]["completed_walks"] == 1


# ---------------------------------------------------------------------------
# ajuste por rating
# ---------------------------------------------------------------------------

def test_rating_acima_de_4_aumenta_score():
    db = _db()
    _review(db, "w1", 5)
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + round((5-4)*8) = 78
    assert result["operational_score"] == 78
    assert result["score_details"]["rating_avg"] == 5
    assert result["score_details"]["rating_count"] == 1
    assert "Média de avaliação 5.0 em 1 avaliação(ões)." in result["score_factors"]["positivos"]


def test_rating_abaixo_de_4_reduz_score():
    db = _db()
    _review(db, "w1", 2)
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + round((2-4)*8) = 70 - 16 = 54
    assert result["operational_score"] == 54
    assert result["score_details"]["rating_avg"] == 2


def test_rating_media_arredonda_para_duas_casas():
    db = _db()
    _review(db, "w1", 4)
    _review(db, "w1", 5)
    _review(db, "w1", 5)
    result = svc.calculate_walker_operational_score("w1", db)
    # media = 14/3 = 4.666... -> round(4.67) ; ajuste round((4.67-4)*8)=round(5.36)=5
    assert result["score_details"]["rating_avg"] == 4.67
    assert result["score_details"]["rating_count"] == 3
    assert result["operational_score"] == 75


# ---------------------------------------------------------------------------
# eventos operacionais
# ---------------------------------------------------------------------------

def test_evento_walker_late_penaliza_apenas_atencao():
    db = _db()
    _event(db, "w1", "walker_late")
    result = svc.calculate_walker_operational_score("w1", db)
    # walker_late esta em ATTENTION mas nao em HIGH e severity low.
    # 70 - (1 atencao * 5) - (0 high *4) = 65
    assert result["operational_score"] == 65
    assert result["score_details"]["recent_operational_events"] == 1
    assert result["score_details"]["high_attention_events"] == 0
    assert "1 evento(s) operacional(is) recente(s) em acompanhamento." in result["score_factors"]["pontos_de_atencao"]


def test_evento_no_show_penaliza_atencao_e_high():
    db = _db()
    _event(db, "w1", "walker_no_show")
    result = svc.calculate_walker_operational_score("w1", db)
    # no_show conta em ATTENTION (-5) e em HIGH (-4): 70 - 5 - 4 = 61
    assert result["operational_score"] == 61
    assert result["score_details"]["recent_operational_events"] == 1
    assert result["score_details"]["high_attention_events"] == 1


def test_evento_severity_high_conta_como_high_mesmo_fora_da_lista():
    db = _db()
    # event_type fora de ATTENTION/HIGH mas severity high -> so high_attention
    _event(db, "w1", "tutor_complaint", severity="high")
    result = svc.calculate_walker_operational_score("w1", db)
    # nao esta em ATTENTION (recent_operational_events=0) mas conta como high (-4)
    assert result["score_details"]["recent_operational_events"] == 0
    assert result["score_details"]["high_attention_events"] == 1
    assert result["operational_score"] == 66


def test_evento_antigo_alem_de_90_dias_ignorado():
    db = _db()
    _event(db, "w1", "walker_late", days_ago=120)
    result = svc.calculate_walker_operational_score("w1", db)
    assert result["score_details"]["recent_operational_events"] == 0
    assert result["operational_score"] == 70


# ---------------------------------------------------------------------------
# rejeicoes de finalizacao
# ---------------------------------------------------------------------------

def test_rejeicao_de_finalizacao_penaliza_6():
    db = _db()
    _rejection(db, "w1", status="rejected")
    result = svc.calculate_walker_operational_score("w1", db)
    assert result["score_details"]["completion_rejections"] == 1
    assert result["operational_score"] == 64  # 70 - 6
    assert "1 finalização(ões) rejeitada(s) para ajuste." in result["score_factors"]["pontos_de_atencao"]


def test_rejeicao_status_completion_rejected_tambem_conta():
    db = _db()
    _rejection(db, "w1", status="completion_rejected")
    result = svc.calculate_walker_operational_score("w1", db)
    assert result["score_details"]["completion_rejections"] == 1


def test_rejeicao_outro_status_nao_conta():
    db = _db()
    _rejection(db, "w1", status="pending_review")
    result = svc.calculate_walker_operational_score("w1", db)
    assert result["score_details"]["completion_rejections"] == 0
    assert result["operational_score"] == 70


# ---------------------------------------------------------------------------
# clamp do score
# ---------------------------------------------------------------------------

def test_score_nao_passa_de_100():
    db = _db()
    for _ in range(20):
        _walk(db, "w1")
    for _ in range(10):
        _review(db, "w1", 5)
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + 12 + 5 + 8 = 95 -> ainda dentro, mas garante <= 100
    assert result["operational_score"] <= 100


def test_score_nao_fica_negativo():
    db = _db()
    for _ in range(10):
        _event(db, "w1", "walker_no_show", severity="high")
    for _ in range(10):
        _rejection(db, "w1")
    result = svc.calculate_walker_operational_score("w1", db)
    assert result["operational_score"] == 0


# ---------------------------------------------------------------------------
# reliability_label nos cortes
# ---------------------------------------------------------------------------

def test_label_em_formacao_com_menos_de_3_passeios():
    db = _db()
    for _ in range(2):
        _walk(db, "w1")
    result = svc.calculate_walker_operational_score("w1", db)
    # completed_count < 3 sempre "Em formação" independente do score
    assert result["score_details"]["completed_walks"] == 2
    assert result["reliability_label"] == "Em formação"


def test_label_muito_confiavel_score_alto():
    db = _db()
    for _ in range(10):
        _walk(db, "w1")
    _review(db, "w1", 5)
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + 12 + 5 + 8 = 95 -> >=88 e completed>=3 -> Muito confiável
    assert result["operational_score"] == 95
    assert result["reliability_label"] == "Muito confiável"


def test_label_confiavel_score_intermediario():
    db = _db()
    for _ in range(5):
        _walk(db, "w1")
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + 10 = 80 ; completed>=3, score entre 60 e 88 -> Confiável
    assert result["operational_score"] == 80
    assert result["reliability_label"] == "Confiável"


def test_label_atencao_operacional_por_score_baixo():
    db = _db()
    for _ in range(3):
        _walk(db, "w1")
    for _ in range(5):
        _review(db, "w1", 1)
    result = svc.calculate_walker_operational_score("w1", db)
    # 70 + 6 + round((1-4)*8)= 70+6-24 = 52 < 60 -> Atenção operacional
    assert result["operational_score"] == 52
    assert result["reliability_label"] == "Atenção operacional"


def test_label_atencao_operacional_por_contagem_de_atencao():
    db = _db()
    for _ in range(5):
        _walk(db, "w1")
    # 3 eventos walker_late: attention_count usado no label = atencao + rejeicoes
    for _ in range(3):
        _event(db, "w1", "walker_late")
    result = svc.calculate_walker_operational_score("w1", db)
    # score: 70 + 10 - (3*5) - 0 = 65 (>=60), mas attention_count=3 -> Atenção operacional
    assert result["operational_score"] == 65
    assert result["reliability_label"] == "Atenção operacional"


def test_attention_count_do_label_inclui_rejeicoes():
    db = _db()
    for _ in range(5):
        _walk(db, "w1")
    # 2 eventos de atencao + 1 rejeicao = 3 no attention_count do label
    _event(db, "w1", "walker_late")
    _event(db, "w1", "walker_late")
    _rejection(db, "w1")
    result = svc.calculate_walker_operational_score("w1", db)
    # score = 70 + 10 - (2*5) - 6 = 64 (>=60) ; attention_count = 2+1 = 3 -> Atenção
    assert result["operational_score"] == 64
    assert result["reliability_label"] == "Atenção operacional"


def test_isolamento_por_walker():
    db = _db()
    for _ in range(5):
        _walk(db, "w1")
    _walk(db, "w2")
    result = svc.calculate_walker_operational_score("w2", db)
    assert result["score_details"]["completed_walks"] == 1
