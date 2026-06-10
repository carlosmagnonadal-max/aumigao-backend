from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.complaint import Complaint, RiskScore
from app.schemas.complaint import ComplaintCreate, ComplaintEvidenceCreate
from app.services import complaint_service as svc


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            Complaint.__table__,
            RiskScore.__table__,
        ],
    )
    return sessionmaker(bind=engine)()


def _payload(**kwargs) -> ComplaintCreate:
    base = dict(
        source="tutor",
        target_type="walker",
        category="atraso",
        title="",
        description="Descricao generica e neutra sem termos.",
    )
    base.update(kwargs)
    return ComplaintCreate(**base)


def _add_complaint(db, *, target_user_id=None, target_pet_id=None, created_at=None):
    c = Complaint(
        id=str(uuid4()),
        source="tutor",
        author_id="author",
        author_role="tutor",
        target_type="walker",
        target_user_id=target_user_id,
        target_pet_id=target_pet_id,
        category="atraso",
        title="x",
        description="historico",
        created_at=created_at or datetime.utcnow(),
    )
    db.add(c)
    db.commit()
    return c


# ---------- classify_complaint: score & severity ----------

def test_baseline_score_is_baixa():
    db = _db()
    # neutral text, non high-risk category, no evidences, no targets => score 10 baixa
    payload = _payload(category="duvida", description="Tenho uma duvida simples e neutra.")
    result = svc.classify_complaint(payload, db)
    assert result["score"] == 10
    assert result["severity"] == "baixa"
    assert result["recurrence_count"] == 0


def test_high_risk_category_adds_points_media():
    db = _db()
    # high risk category "falta_cuidado" (+18) but neutral text => 10+18 = 28 -> baixa
    payload = _payload(
        category="falta_cuidado",
        target_user_id=None,
        description="Relato neutro de servico, nenhuma palavra-chave forte.",
    )
    result = svc.classify_complaint(payload, db)
    assert result["score"] == 28
    assert result["severity"] == "baixa"


def test_critical_term_triggers_high_score():
    db = _db()
    # high risk category falta_cuidado (+18) + critical term "mordida" (+34) = 62 -> alta
    payload = _payload(
        category="falta_cuidado",
        description="O pet sofreu uma mordida grave durante o passeio.",
    )
    result = svc.classify_complaint(payload, db)
    # base 10 + 18 + 34 = 62
    assert result["score"] == 62
    assert result["severity"] == "alta"


def test_critica_severity_with_evidences_and_recurrence():
    db = _db()
    # high risk (+18) + critical term (+34) + evidence (+14) = 76 -> critica
    payload = _payload(
        category="agressividade_pet",
        description="O pet teve um episodio de agressividade com mordida.",
        evidences=[ComplaintEvidenceCreate(evidence_type="photo", url="http://x")],
    )
    result = svc.classify_complaint(payload, db)
    assert result["score"] == 76
    assert result["severity"] == "critica"


def test_medium_term_adds_points():
    db = _db()
    # non high-risk category + medium term "atraso" in description (+18) = 28
    payload = _payload(
        category="duvida",
        description="Houve um atraso na chegada do passeador hoje.",
    )
    result = svc.classify_complaint(payload, db)
    assert result["score"] == 28


def test_score_capped_at_100():
    db = _db()
    pet_id = "petX"
    # create >=3 prior complaints to push recurrence to max 30
    for _ in range(5):
        _add_complaint(db, target_pet_id=pet_id)
    payload = _payload(
        category="agressividade_pet",  # +18
        target_type="pet",
        target_pet_id=pet_id,
        description="agressividade e mordida com atraso e endereco inseguro",  # critical +34, medium +18
        evidences=[ComplaintEvidenceCreate(evidence_type="photo")],  # +14
    )
    result = svc.classify_complaint(payload, db)
    # 10+18+34+18+14+30 = 124 -> capped 100
    assert result["score"] == 100
    assert result["severity"] == "critica"


# ---------- recurrence ----------

def test_recurrence_count_within_window():
    db = _db()
    uid = "userR"
    _add_complaint(db, target_user_id=uid)
    _add_complaint(db, target_user_id=uid)
    payload = _payload(target_user_id=uid, category="duvida",
                       description="Relato neutro adicional sem termos.")
    result = svc.classify_complaint(payload, db)
    assert result["recurrence_count"] == 2
    assert "generate_recurrence_alert" in result["suggestions"]


def test_recurrence_ignores_old_complaints():
    db = _db()
    uid = "userOld"
    old = datetime.utcnow() - timedelta(days=120)
    _add_complaint(db, target_user_id=uid, created_at=old)
    payload = _payload(target_user_id=uid, category="duvida",
                       description="Relato neutro adicional sem termos.")
    result = svc.classify_complaint(payload, db)
    assert result["recurrence_count"] == 0
    assert "generate_recurrence_alert" not in result["suggestions"]


# ---------- suggestions ----------

def test_baseline_suggestions_always_present():
    db = _db()
    payload = _payload(category="duvida", description="Relato neutro e simples sem nada.")
    result = svc.classify_complaint(payload, db)
    assert result["suggestions"][:2] == ["open_admin_case", "register_risk_history"]
    # applied only contains LIGHT_AUTO_ACTIONS
    assert set(result["applied"]) <= {"open_admin_case", "register_risk_history", "generate_recurrence_alert"}


def test_off_platform_attempt_suggested_by_category():
    db = _db()
    payload = _payload(category="contratacao_por_fora",
                       description="O passeador pediu para contratar diretamente.")
    result = svc.classify_complaint(payload, db)
    assert "register_off_platform_attempt" in result["suggestions"]


def test_off_platform_attempt_suggested_by_text():
    db = _db()
    payload = _payload(category="duvida",
                       description="Ele quer fechar negocio fora do app sem registro.")
    result = svc.classify_complaint(payload, db)
    assert "register_off_platform_attempt" in result["suggestions"]


def test_coupon_abuse_flagged():
    db = _db()
    payload = _payload(category="duvida",
                       description="Suspeita de uso indevido de cupom de desconto.")
    result = svc.classify_complaint(payload, db)
    assert "flag_coupon_abuse_review" in result["suggestions"]


def test_block_pet_shared_walk_for_walker_pet_high_severity():
    db = _db()
    # source walker, target_type pet, severity alta/critica -> block_pet_shared_walk
    payload = _payload(
        source="walker",
        target_type="pet",
        category="agressividade_pet",
        target_pet_id="petB",
        description="O pet apresentou agressividade e tentou uma mordida no grupo.",
    )
    result = svc.classify_complaint(payload, db)
    assert result["severity"] in {"alta", "critica"}
    assert "block_pet_shared_walk" in result["suggestions"]
    assert result["requires_manual_review"] is True


def test_no_block_pet_shared_walk_for_low_severity():
    db = _db()
    payload = _payload(
        source="walker",
        target_type="pet",
        category="duvida",
        target_pet_id="petLow",
        description="Apenas uma observacao neutra sobre o pet.",
    )
    result = svc.classify_complaint(payload, db)
    assert result["severity"] in {"baixa", "media"}
    assert "block_pet_shared_walk" not in result["suggestions"]


def test_tutor_high_severity_refund_and_ranking():
    db = _db()
    payload = _payload(
        source="tutor",
        target_type="walker",
        category="falta_cuidado",
        description="Houve falta de cuidado com mordida durante o servico.",
    )
    result = svc.classify_complaint(payload, db)
    assert result["severity"] in {"alta", "critica"}
    assert "review_refund" in result["suggestions"]
    assert "reduce_walker_ranking" in result["suggestions"]


def test_tutor_critica_walker_suspend_and_badge():
    db = _db()
    payload = _payload(
        source="tutor",
        target_type="walker",
        category="agressividade_pet",
        description="agressividade com mordida e fuga, situacao inseguro grave",
        evidences=[ComplaintEvidenceCreate(evidence_type="photo")],
    )
    result = svc.classify_complaint(payload, db)
    assert result["severity"] == "critica"
    assert "temporarily_suspend_walker" in result["suggestions"]
    assert "remove_quality_badge" in result["suggestions"]


def test_walker_critica_tutor_suspend():
    db = _db()
    payload = _payload(
        source="walker",
        target_type="tutor",
        category="endereco_inseguro",
        description="endereco inseguro com ameaça e violencia, situacao critica",
        evidences=[ComplaintEvidenceCreate(evidence_type="note")],
    )
    result = svc.classify_complaint(payload, db)
    assert result["severity"] == "critica"
    assert "temporarily_suspend_tutor" in result["suggestions"]


def test_quality_recovery_for_relevant_categories():
    db = _db()
    payload = _payload(
        source="tutor",
        target_type="walker",
        category="comunicacao_inadequada",
        description="Houve atraso e comunicacao ruim durante o passeio.",
    )
    result = svc.classify_complaint(payload, db)
    assert result["severity"] in {"media", "alta", "critica"}
    assert "start_quality_recovery" in result["suggestions"]


# ---------- _upsert_risk ----------

def test_upsert_risk_creates_record_and_blocks_pet_high():
    db = _db()
    svc._upsert_risk("pet", "pet1", "alta", db)
    db.commit()
    risk = db.query(RiskScore).filter_by(subject_type="pet", subject_id="pet1").first()
    assert risk is not None
    assert risk.score == 24  # alta points
    assert risk.complaints_count == 1
    assert risk.shared_walk_blocked is True
    assert risk.severity == "atencao"  # 24 -> >=20


def test_upsert_risk_pet_critica_blocks_and_critical_count():
    db = _db()
    svc._upsert_risk("pet", "pet2", "critica", db)
    db.commit()
    risk = db.query(RiskScore).filter_by(subject_type="pet", subject_id="pet2").first()
    assert risk.score == 40
    assert risk.critical_count == 1
    assert risk.shared_walk_blocked is True
    assert risk.severity == "critico"  # critical_count truthy


def test_upsert_risk_pet_low_severity_not_blocked():
    db = _db()
    svc._upsert_risk("pet", "pet3", "media", db)
    db.commit()
    risk = db.query(RiskScore).filter_by(subject_type="pet", subject_id="pet3").first()
    assert risk.shared_walk_blocked is False
    assert risk.score == 12


def test_upsert_risk_walker_high_not_blocked():
    db = _db()
    # shared_walk_blocked only flips for subject_type == "pet"
    svc._upsert_risk("walker", "w1", "critica", db)
    db.commit()
    risk = db.query(RiskScore).filter_by(subject_type="walker", subject_id="w1").first()
    assert risk.shared_walk_blocked is False
    assert risk.critical_count == 1
    assert risk.severity == "critico"


def test_upsert_risk_accumulates_and_keeps_block_sticky():
    db = _db()
    # first high -> blocked
    svc._upsert_risk("pet", "pet4", "alta", db)
    db.commit()
    # then low -> block stays True (sticky via "or")
    svc._upsert_risk("pet", "pet4", "baixa", db)
    db.commit()
    risk = db.query(RiskScore).filter_by(subject_type="pet", subject_id="pet4").first()
    assert risk.complaints_count == 2
    assert risk.score == 29  # 24 + 5
    assert risk.shared_walk_blocked is True


def test_upsert_risk_score_capped_at_100():
    db = _db()
    for _ in range(4):
        svc._upsert_risk("pet", "pet5", "critica", db)
    db.commit()
    risk = db.query(RiskScore).filter_by(subject_type="pet", subject_id="pet5").first()
    # 40*4 = 160 capped at 100
    assert risk.score == 100
    assert risk.critical_count == 4


def test_upsert_risk_none_subject_noop():
    db = _db()
    svc._upsert_risk("pet", None, "critica", db)
    db.commit()
    assert db.query(RiskScore).count() == 0
