"""api-T2 — schema da acao administrativa legada de ocorrencias (complaints.py).

O endpoint POST /admin/occurrences/{id}/action trocou payload: dict por schema
Pydantic permissivo. Estes testes garantem que nenhum payload existente e rejeitado
(campos opcionais + extras ignorados) — so ganhamos validacao de tipo e contrato OpenAPI.
"""
from app.routes.complaints import LegacyOccurrenceActionRequest


def test_action_accepts_empty_payload():
    m = LegacyOccurrenceActionRequest()
    assert m.action is None
    assert m.note is None


def test_action_ignores_extra_fields():
    m = LegacyOccurrenceActionRequest(**{"action": "mark_resolved", "campo_extra": 1})
    assert m.action == "mark_resolved"


def test_action_accepts_full_payload():
    m = LegacyOccurrenceActionRequest(action="add_internal_note", note="anotacao")
    assert m.action == "add_internal_note"
    assert m.note == "anotacao"
