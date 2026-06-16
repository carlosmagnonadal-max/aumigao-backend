"""api-T2 — schemas dos endpoints do passeador (walker.py).

Os endpoints da maquina de estados trocaram payload: dict por schema Pydantic. Estes
testes garantem que os schemas sao PERMISSIVOS (campos opcionais + extras ignorados) e
que a semantica do `key in payload` (incluir no log so as chaves enviadas, mesmo False)
foi preservada via _collect_checklist_items + model_fields_set.
"""
from app.routes.walker import (
    CompletionReportRequest,
    WalkerChecklistInput,
    WalkExperienceInput,
    _collect_checklist_items,
    _normalize_completion_checklist,
)


def test_checklist_empty_payload():
    m = WalkerChecklistInput()
    assert m.checklist_confirm_water is None
    # Nada enviado -> nenhum item coletado.
    assert _collect_checklist_items(m) == {}


def test_checklist_none_payload():
    # Body ausente (None) -> dict vazio, sem explodir.
    assert _collect_checklist_items(None) == {}


def test_checklist_ignores_extra_fields():
    m = WalkerChecklistInput(**{"checklist_confirm_water": True, "campo_extra": 99})
    assert m.checklist_confirm_water is True
    assert _collect_checklist_items(m) == {"checklist_confirm_water": True}


def test_checklist_includes_false_when_sent():
    # Semantica do `key in payload`: chave enviada com False ENTRA no log.
    m = WalkerChecklistInput(**{"checklist_confirm_bags": False})
    assert _collect_checklist_items(m) == {"checklist_confirm_bags": False}


def test_checklist_omits_unsent_keys():
    m = WalkerChecklistInput(checklist_confirm_water=True)
    collected = _collect_checklist_items(m)
    assert collected == {"checklist_confirm_water": True}
    assert "checklist_confirm_bowl" not in collected


def test_experience_defaults_false():
    m = WalkExperienceInput()
    assert m.did_pee is False
    assert m.did_poop is False


def test_experience_accepts_values_and_ignores_extra():
    m = WalkExperienceInput(**{"did_pee": True, "did_poop": True, "extra": 1})
    assert m.did_pee is True
    assert m.did_poop is True


# --- /walks/{id}/completion-report e /report -----------------------------------
# Os endpoints convertem para dict (model_dump) e passam ao helper inalterado. Os
# testes garantem que o roundtrip schema->dict preserva o que o helper espera.

def test_completion_report_empty_model_dump_keeps_keys_none():
    d = CompletionReportRequest().model_dump()
    # Helper faz payload.get("photo_url") etc. -> precisa das chaves presentes (None).
    assert d == {"photo_url": None, "url": None, "notes": None, "checklist": None, "checklist_json": None}


def test_completion_report_ignores_extra_fields():
    m = CompletionReportRequest(**{"photo_url": "http://x", "campo_extra": 1})
    assert m.photo_url == "http://x"


def test_completion_report_checklist_accepts_dict_and_string():
    # checklist e Any -> tanto dict quanto string JSON passam sem 422 (helper normaliza).
    as_dict = CompletionReportRequest(checklist={"pet_delivered": True})
    as_str = CompletionReportRequest(checklist="{\"pet_delivered\": true}")
    assert as_dict.checklist == {"pet_delivered": True}
    assert isinstance(as_str.checklist, str)


def test_completion_report_normalize_checklist_roundtrip():
    # Caminho real: schema -> model_dump -> _normalize_completion_checklist.
    full_checklist = {"pet_delivered": True, "leash_returned": True, "water_offered": False, "incident_reported": False}
    d = CompletionReportRequest(photo_url="http://x", notes="ok", checklist=full_checklist).model_dump()
    normalized = _normalize_completion_checklist(d)
    assert normalized == {"pet_delivered": True, "leash_returned": True, "water_offered": False, "incident_reported": False}
