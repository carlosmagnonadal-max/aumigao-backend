"""api-T2 — schemas dos endpoints administrativos (admin.py).

Varios endpoints do admin trocaram payload: dict por schema Pydantic permissivo. Estes
testes garantem que nenhum payload existente e rejeitado (campos opcionais + extras
ignorados pelo Pydantic v2) — so ganhamos validacao de tipo e contrato no OpenAPI.

Os endpoints de settings (referral-program/walker-programs) NAO foram migrados de
proposito: usam _merge_dict com dict livre aninhado, onde um schema descartaria chaves.
"""
from app.routes.admin import (
    AdjustWalkerCrRequest,
    AdminWalkStatusRequest,
    KitAuditActionRequest,
    OperationalEventRequest,
    ReferralStatusRequest,
    RejectWalkerKitRequest,
    RejectWalkerRequest,
    SetWalkerWalletRequest,
    TipReviewActionRequest,
    UpdatePartnerApplicationRequest,
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


# --- acoes do programa de passeadores (defaults preservados) --------------------

def test_referral_status_defaults():
    m = ReferralStatusRequest()
    assert m.status is None
    assert m.note == ""


def test_adjust_cr_defaults_and_coercion():
    m = AdjustWalkerCrRequest()
    assert m.amount == 0
    assert m.reason == "Ajuste administrativo"
    # Pydantic coage string numerica -> int.
    assert AdjustWalkerCrRequest(amount="5").amount == 5


def test_kit_audit_defaults():
    m = KitAuditActionRequest()
    assert m.status == "aprovado"
    assert m.note == ""


def test_tip_review_defaults_and_ignores_extra():
    m = TipReviewActionRequest(**{"extra": 1})
    assert m.status == "approved"
    assert m.note == ""


# --- evento operacional / rejeicao / status / wallet / candidatura --------------

def test_operational_event_model_dump_keeps_keys_for_helper():
    # O helper faz payload.get(...) sobre o dict -> precisa das chaves presentes (None).
    d = OperationalEventRequest(entity_type="walk", entity_id="w1", title="t").model_dump()
    assert d["entity_type"] == "walk"
    assert d["metadata"] is None  # helper trata None -> {}
    assert set(d.keys()) == {
        "entity_type", "entity_id", "title", "event_type", "severity", "description", "source", "metadata",
    }


def test_reject_walker_reason_optional():
    assert RejectWalkerRequest().reason is None
    assert RejectWalkerRequest(reason="docs invalidos").reason == "docs invalidos"


def test_admin_walk_status_optional():
    assert AdminWalkStatusRequest().status is None
    assert AdminWalkStatusRequest(status="ride_scheduled").status == "ride_scheduled"


def test_partner_application_exclude_unset_preserves_patch_semantics():
    # So as chaves enviadas aparecem -> "x in data" se comporta como o dict original.
    only_notes = UpdatePartnerApplicationRequest(internal_notes="ok").model_dump(exclude_unset=True)
    assert only_notes == {"internal_notes": "ok"}
    assert "status" not in only_notes
    # Enviar active_as_walker=False ainda conta como "presente".
    with_flag = UpdatePartnerApplicationRequest(active_as_walker=False).model_dump(exclude_unset=True)
    assert with_flag == {"active_as_walker": False}


def test_set_wallet_requires_key_via_fields_set():
    # Chave ausente -> nao esta em model_fields_set (endpoint devolve 422).
    assert "asaas_wallet_id" not in SetWalkerWalletRequest().model_fields_set
    # Chave enviada como null (para limpar) -> presente em model_fields_set.
    explicit = SetWalkerWalletRequest(**{"asaas_wallet_id": None})
    assert "asaas_wallet_id" in explicit.model_fields_set
    # Chave enviada com valor.
    set_val = SetWalkerWalletRequest(**{"asaas_wallet_id": "wlt_123"})
    assert set_val.asaas_wallet_id == "wlt_123"
    assert "asaas_wallet_id" in set_val.model_fields_set
