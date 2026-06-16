"""api-T2 — schemas das ocorrências de passeio (walks.py).

Os endpoints /walks/{id}/complaint e /kit-issue-report trocaram payload: dict por
schema Pydantic. Estes testes garantem que os schemas são PERMISSIVOS: nenhum payload
que os apps já enviam é rejeitado (campos opcionais + extras ignorados) — só ganhamos
validação de tipo, 422 e contrato no OpenAPI.
"""
from app.routes.walks import (
    FORBIDDEN_RESCHEDULE_FIELDS,
    RescheduleSelectedWalkerRequest,
    WalkComplaintRequest,
    WalkKitIssueReportRequest,
)


def test_complaint_accepts_empty_payload():
    m = WalkComplaintRequest()
    assert m.target_type is None
    assert m.evidences == []
    assert m.metadata is None


def test_complaint_ignores_extra_fields():
    # Pydantic v2 ignora extras por padrão — app que mande campo a mais não toma 422.
    m = WalkComplaintRequest(**{"title": "Reclamacao", "campo_desconhecido": 123, "notes": "x"})
    assert m.title == "Reclamacao"
    assert m.notes == "x"


def test_complaint_accepts_full_payload():
    m = WalkComplaintRequest(
        target_type="walker",
        target_user_id="u1",
        target_pet_id="p1",
        category="servico",
        title="t",
        description="d",
        evidences=[{"url": "http://x", "kind": "photo"}],
        metadata={"k": "v"},
    )
    assert m.evidences[0]["url"] == "http://x"
    assert m.metadata == {"k": "v"}


def test_kit_issue_confirm_defaults_false():
    assert WalkKitIssueReportRequest().confirm_report is False


def test_kit_issue_accepts_payload():
    m = WalkKitIssueReportRequest(confirm_report=True, missing_items={"agua": False}, notes="n")
    assert m.confirm_report is True
    assert m.missing_items == {"agua": False}


# --- /walks/{id}/reschedule-selected-walker -------------------------------------
# O endpoint troca payload: dict por schema, mas PRECISA preservar a rejeicao explicita
# (400) de campos proibidos. Antes era FORBIDDEN.intersection(payload.keys()); agora e
# FORBIDDEN.intersection(payload.model_fields_set). Estes testes travam essa semantica.

def test_reschedule_accepts_date_fields():
    m = RescheduleSelectedWalkerRequest(scheduled_date="2026-07-01", walk_date="2026-07-01", walk_time="08:00")
    assert m.scheduled_date == "2026-07-01"
    assert m.walk_time == "08:00"
    # Apenas data/horario foram setados — nenhum campo proibido detectado.
    assert not FORBIDDEN_RESCHEDULE_FIELDS.intersection(m.model_fields_set)


def test_reschedule_ignores_unknown_extra():
    # Extra desconhecido continua ignorado (Pydantic v2) — nao quebra apps.
    m = RescheduleSelectedWalkerRequest(**{"scheduled_date": "2026-07-01", "campo_qualquer": 1})
    assert m.scheduled_date == "2026-07-01"
    assert not FORBIDDEN_RESCHEDULE_FIELDS.intersection(m.model_fields_set)


def test_reschedule_detects_forbidden_field_even_when_null():
    # Mesma semantica do payload.keys(): presenca da chave (mesmo com valor nulo) e proibida.
    m = RescheduleSelectedWalkerRequest(**{"scheduled_date": "2026-07-01", "price": None})
    assert "price" in m.model_fields_set
    assert FORBIDDEN_RESCHEDULE_FIELDS.intersection(m.model_fields_set) == {"price"}


def test_reschedule_forbidden_field_does_not_422_on_bad_type():
    # Campos proibidos sao Any -> qualquer tipo entra (vira 400 logico depois, nao 422).
    m = RescheduleSelectedWalkerRequest(**{"walker_id": 12345, "duration_minutes": "abc"})
    assert FORBIDDEN_RESCHEDULE_FIELDS.intersection(m.model_fields_set) == {"walker_id", "duration_minutes"}
