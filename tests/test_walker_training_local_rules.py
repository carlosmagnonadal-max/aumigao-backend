"""S2 — M10-B: módulos locais por UF/cidade do passeador + regras estruturadas do S3 (se existir).

Contrato do S3 (plano s3, DV2): local_rules_service.rules_for_city(db, municipio, uf) -> list[LocalRule].
"""
import sys
from datetime import date
from types import SimpleNamespace

from app.services import walker_training_service as svc
from tests.training_helpers import WALKER_ID, configure_training, make_bundle, make_db, write_bundle

_S3 = "app.services.local_rules_service"


def _setup(tmp_path, monkeypatch, s3=None, **db_kwargs):
    write_bundle(tmp_path, make_bundle(status="draft"))
    configure_training(monkeypatch, tmp_path, mode="warn")
    monkeypatch.setitem(sys.modules, _S3, s3 if s3 is not None else SimpleNamespace())
    return make_db(**db_kwargs)


def test_salvador_walker_gets_local_module_and_no_rules_without_s3(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch, city="Salvador", state="BA")
    data = svc.local_rules(db, WALKER_ID)
    assert data["city"] == "Salvador"
    assert data["uf"] == "BA"
    assert [m["id"] for m in data["modules"]] == ["m10b-salvador"]
    assert data["modules"][0]["passed"] is False
    assert data["rules"] == []


def test_other_city_or_uf_gets_nothing(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch, city="Recife", state="PE")
    assert svc.local_rules(db, WALKER_ID)["modules"] == []


def test_accent_case_and_state_not_uf(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch, city="  SALVADOR ", state="pituba")
    data = svc.local_rules(db, WALKER_ID)
    assert data["uf"] is None
    assert [m["id"] for m in data["modules"]] == ["m10b-salvador"]


def _s3_rule(**overrides):
    """Imita app.models.local_rule.LocalRule do S3 (ORM), só com os atributos lidos pelo S2."""
    base = dict(
        id="lr-ba-salvador-focinheira-peso", uf="BA", municipio="Salvador", nivel="municipal",
        tema="focinheira", norma="Lei Municipal nº 9.108/2016 (Salvador), arts. 5º e 11",
        fonte_url="https://exemplo.gov.br/lei", verificado_em=date(2026, 9, 15), confianca="alta",
        status="vigente", criterio="weight_min_kg", params_json='{"min_kg": 24}',
        exigencia_json='{"itens": ["guia", "focinheira"], "onde": "em local público", '
                       '"detalhe": "Cães acima de 24 kg em local público."}',
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_structured_rules_come_from_s3_when_available(tmp_path, monkeypatch):
    calls = []

    def rules_for_city(db, municipio, uf, *, include_tramitacao=False, cache=None):
        calls.append((municipio, uf))
        return [
            _s3_rule(),
            _s3_rule(id="lr-ba-salvador-praia", tema="praia", status="conflito", confianca="baixa",
                     exigencia_json='{"itens": ["guia"], "onde": "na praia"}'),
        ]

    db = _setup(tmp_path, monkeypatch, s3=SimpleNamespace(rules_for_city=rules_for_city))
    data = svc.local_rules(db, WALKER_ID)
    assert calls == [("Salvador", "BA")]
    assert data["rules"][0] == {
        "id": "lr-ba-salvador-focinheira-peso",
        "tema": "focinheira",
        "nivel": "municipal",
        "municipio": "Salvador",
        "uf": "BA",
        "status": "vigente",
        "confianca": "alta",
        "norma": "Lei Municipal nº 9.108/2016 (Salvador), arts. 5º e 11",
        "fonte_url": "https://exemplo.gov.br/lei",
        "verificado_em": "2026-09-15",
        "itens": ["guia", "focinheira"],
        "resumo": "Cães acima de 24 kg em local público.",
        "nota": None,
    }
    # C3: regra em conflito nunca soa como permissão — texto de cautela, não "guia na praia".
    assert data["rules"][1]["resumo"] == (
        "Praia: situação legal em conflito — evite levar o cão à praia até confirmação; se for, guia."
    )
    assert data["rules"][1]["status"] == "conflito"


def test_s3_failure_returns_empty_rules(tmp_path, monkeypatch):
    def broken(db, municipio, uf, **kwargs):
        raise RuntimeError("tabela ausente")

    db = _setup(tmp_path, monkeypatch, s3=SimpleNamespace(rules_for_city=broken))
    assert svc.local_rules(db, WALKER_ID)["rules"] == []


def test_s3_module_missing_returns_empty_rules(tmp_path, monkeypatch):
    db = _setup(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, _S3, None)  # import falha (ModuleNotFoundError/ImportError)
    assert svc.local_rules(db, WALKER_ID)["rules"] == []
