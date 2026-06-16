"""api-T2 — schemas das ocorrências de passeio (walks.py).

Os endpoints /walks/{id}/complaint e /kit-issue-report trocaram payload: dict por
schema Pydantic. Estes testes garantem que os schemas são PERMISSIVOS: nenhum payload
que os apps já enviam é rejeitado (campos opcionais + extras ignorados) — só ganhamos
validação de tipo, 422 e contrato no OpenAPI.
"""
from app.routes.walks import WalkComplaintRequest, WalkKitIssueReportRequest


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
