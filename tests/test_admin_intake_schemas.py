"""api-T2 — schemas dos endpoints administrativos (admin.py).

Varios endpoints do admin trocaram payload: dict por schema Pydantic permissivo. Estes
testes garantem que nenhum payload existente e rejeitado (campos opcionais + extras
ignorados pelo Pydantic v2) — so ganhamos validacao de tipo e contrato no OpenAPI.

Os endpoints de settings (referral-program/walker-programs) NAO foram migrados de
proposito: usam _merge_dict com dict livre aninhado, onde um schema descartaria chaves.
"""
from app.routes.admin import (
    RejectWalkerKitRequest,
    WalkCompletionDecisionRequest,
)


def test_completion_decision_empty():
    m = WalkCompletionDecisionRequest()
    assert m.admin_note is None
    assert m.note is None
    assert m.reason is None


def test_completion_decision_ignores_extra():
    m = WalkCompletionDecisionRequest(**{"admin_note": "ok", "campo_extra": 1})
    assert m.admin_note == "ok"


def test_completion_decision_full():
    m = WalkCompletionDecisionRequest(admin_note="a", note="n", reason="r")
    assert (m.admin_note, m.note, m.reason) == ("a", "n", "r")


def test_reject_kit_empty_and_extra():
    assert RejectWalkerKitRequest().audit_note is None
    m = RejectWalkerKitRequest(**{"reason": "incompleto", "extra": 1})
    assert m.reason == "incompleto"
