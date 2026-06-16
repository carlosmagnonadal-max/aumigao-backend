"""api-T2 — schemas dos endpoints do passeador (walker.py).

Os endpoints da maquina de estados trocaram payload: dict por schema Pydantic. Estes
testes garantem que os schemas sao PERMISSIVOS (campos opcionais + extras ignorados) e
que a semantica do `key in payload` (incluir no log so as chaves enviadas, mesmo False)
foi preservada via _collect_checklist_items + model_fields_set.
"""
from app.routes.walker import (
    WalkerChecklistInput,
    WalkExperienceInput,
    _collect_checklist_items,
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
