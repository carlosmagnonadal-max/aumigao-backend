"""S3 — local_rules_service: local do passeio × regras locais × ficha do pet."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.core.database import Base
from app.models.local_rule import LocalRule
from app.models.pet import Pet
from app.models.tenant import TenantUnit
from app.models.tutor_profile import TutorProfile
from app.models.walk import Walk
from app.services import local_rules_service as lrs

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0111_local_rules_pet_reactive.py"
)


def _db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _seed(db):
    spec = importlib.util.spec_from_file_location("mig_0111_seed", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for row in module.SEED_ROWS:
        db.add(LocalRule(**row))
    db.commit()


def _walk(**kw) -> Walk:
    base = dict(id="w1", tutor_id="tutor1", pet_id="pet1", tenant_id="t1",
                scheduled_date="2026-09-20T10:00", duration_minutes=45, price=40.0)
    base.update(kw)
    return Walk(**base)


# ---------------------------------------------------------------- normalização
def test_normalize_text_removes_accents_case_and_extra_spaces():
    assert lrs.normalize_text("  SÃO   Luís ") == "sao luis"
    assert lrs.normalize_text(None) == ""


def test_normalize_uf_accepts_sigla_and_full_state_name():
    assert lrs.normalize_uf("ba") == "BA"
    assert lrs.normalize_uf("Bahia") == "BA"
    assert lrs.normalize_uf("pernambuco") == "PE"
    assert lrs.normalize_uf("XX") is None
    assert lrs.normalize_uf("") is None
    assert lrs.normalize_uf(None) is None


@pytest.mark.parametrize("text,expected", [
    ("Rua A, 10 — Pituba, Salvador/BA · CEP 41810-000", ("Salvador", "BA")),
    ("Av. Oceânica, 123 - Barra, Salvador - BA, 40140-130, Brasil", ("Salvador", "BA")),
    ("Praça X, Lauro de Freitas - BA", ("Lauro de Freitas", "BA")),
    ("Ed. Solar - Bloco B/AP 101", (None, None)),
    ("Parque da Cidade, perto do lago", (None, None)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_parse_city_uf(text, expected):
    assert lrs.parse_city_uf(text) == expected


# ------------------------------------------------------- resolução do local
def test_resolve_prefers_meeting_point_then_tutor_profile():
    db = _db()
    db.add(TutorProfile(id="tp1", user_id="tutor1", city="Salvador", state="BA"))
    db.add(TenantUnit(tenant_id="t1", name="Matriz", city="Recife", state="PE", status="active"))
    db.commit()
    assert lrs.resolve_walk_location(db, _walk(meeting_point="Praça X, Lauro de Freitas - BA")) == ("Lauro de Freitas", "BA")
    assert lrs.resolve_walk_location(db, _walk(meeting_point="perto do lago")) == ("Salvador", "BA")


def test_resolve_falls_back_to_address_snapshot_when_profile_has_no_city():
    db = _db()
    db.add(TutorProfile(id="tp1", user_id="tutor1", city="", state=""))
    db.commit()
    walk = _walk(address_snapshot="Rua A, 1 — Boa Viagem, Recife/PE · CEP 51020-000")
    assert lrs.resolve_walk_location(db, walk) == ("Recife", "PE")


def test_resolve_falls_back_to_single_city_of_active_tenant_units():
    db = _db()
    db.add(TenantUnit(tenant_id="t1", name="Matriz", city="Salvador", state="BA", status="active"))
    db.add(TenantUnit(tenant_id="t1", name="Filial", city="salvador", state="ba", status="active"))
    db.add(TenantUnit(tenant_id="t1", name="Antiga", city="Recife", state="PE", status="inactive"))
    db.commit()
    assert lrs.resolve_walk_location(db, _walk()) == ("Salvador", "BA")


def test_resolve_returns_none_when_tenant_units_are_in_different_cities():
    db = _db()
    db.add(TenantUnit(tenant_id="t1", name="A", city="Salvador", state="BA", status="active"))
    db.add(TenantUnit(tenant_id="t1", name="B", city="Recife", state="PE", status="active"))
    db.commit()
    assert lrs.resolve_walk_location(db, _walk()) == (None, None)


def test_resolve_tutor_location_uses_profile_then_tenant_units():
    db = _db()
    db.add(TenantUnit(tenant_id="t1", name="Matriz", city="Salvador", state="BA", status="active"))
    db.commit()
    assert lrs.resolve_tutor_location(db, "tutor-sem-perfil", "t1") == ("Salvador", "BA")
    db.add(TutorProfile(id="tp9", user_id="tutor9", city="Recife", state="PE"))
    db.commit()
    assert lrs.resolve_tutor_location(db, "tutor9", "t1") == ("Recife", "PE")


def _pet(**kw) -> Pet:
    base = dict(id="pet1", tutor_id="tutor1", name="Thor", weight=None, size="", breed="", is_reactive=False)
    base.update(kw)
    return Pet(**base)


SSA_32KG = "Salvador: cão de 32 kg — guia e focinheira em local público."


# ------------------------------------------------ critérios de aceite (spec §6)
def test_salvador_dog_32kg_gets_muzzle_alert():
    db = _db(); _seed(db)
    alerts = lrs.alerts_for(db, _pet(weight=32.0), "Salvador", "BA")
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.message == SSA_32KG
    assert alert.tema == "focinheira"
    assert alert.severity == "warning"
    assert alert.title == "Regra local · Salvador"
    assert "9.108/2016" in alert.norma
    assert alert.status == "vigente"
    assert alert.verificado_em == "2026-09-15"


def test_salvador_threshold_is_strictly_above_24kg():
    # Lei 9.108/2016: grande porte = "acima de 24 kg" (comparação estrita).
    db = _db(); _seed(db)
    assert lrs.alerts_for(db, _pet(weight=24.0), "Salvador", "BA") == []
    assert lrs.alerts_for(db, _pet(weight=23.9), "Salvador", "BA") == []
    alerts = lrs.alerts_for(db, _pet(weight=24.1), "Salvador", "BA")
    assert [a.message for a in alerts] == ["Salvador: cão de 24,1 kg — guia e focinheira em local público."]


def test_weight_min_kg_is_inclusive_by_default_when_param_absent():
    db = _db()
    db.add(LocalRule(id="lr-x-inclusivo", uf="PE", municipio="Recife", nivel="municipal", tema="focinheira",
                     criterio="weight_min_kg", params_json=json.dumps({"min_kg": 20}),
                     exigencia_json=json.dumps({"itens": ["focinheira"], "onde": "em via pública"}),
                     norma="Regra de teste a partir de 20 kg", status="vigente"))
    db.commit()
    assert len(lrs.alerts_for(db, _pet(weight=20.0), "Recife", "PE")) == 1


def test_reactive_dog_gets_alert():
    db = _db(); _seed(db)
    alerts = lrs.alerts_for(db, _pet(weight=8.0, is_reactive=True), "Salvador", "BA")
    assert [a.message for a in alerts] == ["Salvador: cão reativo — guia e focinheira em local público."]


def test_city_without_rules_gets_no_alert():
    db = _db(); _seed(db)
    assert lrs.alerts_for(db, _pet(weight=40.0, is_reactive=True), "Feira de Santana", "BA") == []


# ------------------------------------------------------------------- detalhes
def test_city_matching_ignores_accents_and_case():
    db = _db(); _seed(db)
    assert len(lrs.alerts_for(db, _pet(weight=32.0), "SALVADOR", "ba")) == 1


def test_size_fallback_when_weight_is_missing():
    db = _db(); _seed(db)
    alerts = lrs.alerts_for(db, _pet(weight=None, size="Grande"), "Salvador", "BA")
    assert [a.message for a in alerts] == ["Salvador: cão de porte grande — guia e focinheira em local público."]


def test_behavior_with_dogs_reativo_counts_as_reactive():
    db = _db(); _seed(db)
    assert len(lrs.alerts_for(db, _pet(weight=8.0, behavior_with_dogs="reativo"), "Salvador", "BA")) == 1


def test_heavy_and_reactive_dog_gets_both_alerts():
    db = _db(); _seed(db)
    alerts = lrs.alerts_for(db, _pet(weight=30.0, is_reactive=True), "Salvador", "BA")
    assert sorted(a.rule_id for a in alerts) == ["lr-ba-salvador-focinheira-peso", "lr-ba-salvador-focinheira-reativo"]


def test_recife_has_only_location_rules_so_no_pet_alert():
    db = _db(); _seed(db)
    assert lrs.alerts_for(db, _pet(weight=40.0, is_reactive=True), "Recife", "PE") == []
    assert {r.id for r in lrs.rules_for_city(db, "Recife", "PE")} == {"lr-pe-praia-coleira", "lr-pe-praia-dejetos"}


def test_rules_for_city_lists_municipal_first_and_flags_beach_conflict():
    db = _db(); _seed(db)
    rules = lrs.rules_for_city(db, "Salvador", "BA")
    assert {r.id for r in rules} == {
        "lr-ba-salvador-focinheira-peso", "lr-ba-salvador-focinheira-reativo",
        "lr-ba-salvador-guia-afluxo", "lr-ba-salvador-praia", "lr-ba-salvador-dejetos",
    }
    praia = next(r for r in rules if r.id == "lr-ba-salvador-praia")
    assert praia.status == "conflito"
    assert lrs.rules_for_city(db, "Salvador", None) == []


def test_conflict_rule_never_produces_categorical_alert():
    db = _db()
    db.add(LocalRule(id="lr-x-conflito", uf="PE", municipio="Recife", nivel="municipal", tema="focinheira",
                     criterio="weight_min_kg", params_json=json.dumps({"min_kg": 10}),
                     exigencia_json=json.dumps({"itens": ["focinheira"], "onde": "em via pública"}),
                     norma="Norma em conflito", status="conflito"))
    db.commit()
    assert lrs.alerts_for(db, _pet(weight=20.0), "Recife", "PE") == []


def test_tramitacao_rules_are_ignored_unless_requested():
    db = _db()
    db.add(LocalRule(id="lr-x-pl", uf="PE", municipio="Recife", nivel="municipal", tema="focinheira",
                     criterio="weight_min_kg", params_json=json.dumps({"min_kg": 10}),
                     exigencia_json=json.dumps({"itens": ["focinheira"], "onde": "em via pública"}),
                     norma="PL em tramitação", status="tramitacao"))
    db.commit()
    assert lrs.rules_for_city(db, "Recife", "PE") == []
    assert [r.id for r in lrs.rules_for_city(db, "Recife", "PE", include_tramitacao=True)] == ["lr-x-pl"]
    assert lrs.alerts_for(db, _pet(weight=20.0), "Recife", "PE") == []


def test_breed_list_criterion_matches_normalized_breed_for_state_rule():
    db = _db()
    db.add(LocalRule(id="lr-x-sc-raca", uf="SC", municipio=None, nivel="estadual", tema="focinheira",
                     criterio="breed_list", params_json=json.dumps({"breeds": ["Pit Bull"]}),
                     exigencia_json=json.dumps({"itens": ["focinheira"], "onde": "em via pública"}),
                     norma="Lei de teste", status="vigente"))
    db.commit()
    alerts = lrs.alerts_for(db, _pet(breed="pit bull", weight=15.0), "Florianópolis", "SC")
    assert [a.message for a in alerts] == ["SC: raça pit bull — focinheira em via pública."]


def test_malformed_params_json_does_not_crash():
    db = _db()
    db.add(LocalRule(id="lr-x-ruim", uf="BA", municipio="Salvador", nivel="municipal", tema="focinheira",
                     criterio="weight_min_kg", params_json="{isto nao e json", exigencia_json="[]",
                     norma="Regra malformada", status="vigente"))
    db.commit()
    assert lrs.alerts_for(db, _pet(weight=40.0), "Salvador", "BA") == []


def test_safety_alerts_for_walk_uses_walk_location_and_returns_dicts():
    db = _db(); _seed(db)
    db.add(Pet(id="pet1", tutor_id="tutor1", name="Thor", weight=32.0))
    db.add(TutorProfile(id="tp1", user_id="tutor1", city="Salvador", state="BA"))
    db.commit()
    out = lrs.safety_alerts_for_walk(db, _walk())
    assert [a["message"] for a in out] == [SSA_32KG]
    assert set(out[0]) == {"rule_id", "uf", "municipio", "tema", "severity", "title", "message",
                           "norma", "fonte_url", "status", "verificado_em"}


def test_safety_alerts_for_walk_without_location_is_empty():
    db = _db(); _seed(db)
    db.add(Pet(id="pet1", tutor_id="tutor1", name="Thor", weight=32.0))
    db.commit()
    assert lrs.safety_alerts_for_walk(db, _walk()) == []


def test_rules_cache_avoids_second_query(monkeypatch):
    db = _db(); _seed(db)
    cache: dict = {}
    first = lrs.rules_for_city(db, "Salvador", "BA", cache=cache)
    monkeypatch.setattr(db, "query", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sem cache")))
    assert lrs.rules_for_city(db, "salvador", "BA", cache=cache) == first


# --------------------------------------------------------------------------- #
# S3 (Task 5) — limite de cães por passeador (override municipal)
# --------------------------------------------------------------------------- #
from app.models.shared_walk import TenantSharedWalkConfig


def _limit_rule(db, *, rule_id, max_dogs, status="vigente", municipio="Salvador", uf="BA", nivel="municipal"):
    db.add(LocalRule(id=rule_id, uf=uf, municipio=municipio, nivel=nivel, tema="limite_caes",
                     criterio="operational", params_json=json.dumps({"max_dogs": max_dogs}),
                     exigencia_json=json.dumps({"itens": [f"no máximo {max_dogs} cães por passeador"]}),
                     norma="Regra de teste", status=status))
    db.commit()


def test_max_dogs_without_override_returns_national_default():
    db = _db(); _seed(db)
    assert lrs.local_max_dogs_override(db, "Salvador", "BA") is None
    assert lrs.max_dogs_per_walker(db, "Salvador", "BA", national_default=6) == 6


def test_max_dogs_override_uses_most_protective_vigente_value():
    db = _db(); _seed(db)
    _limit_rule(db, rule_id="lr-t-estado", max_dogs=4, municipio=None, nivel="estadual")
    _limit_rule(db, rule_id="lr-t-ssa", max_dogs=2)
    _limit_rule(db, rule_id="lr-t-pl", max_dogs=1, status="tramitacao")
    assert lrs.local_max_dogs_override(db, "Salvador", "BA") == 2
    assert lrs.max_dogs_per_walker(db, "Salvador", "BA", national_default=6) == 2
    assert lrs.local_max_dogs_override(db, "Feira de Santana", "BA") == 4


def test_invalid_max_dogs_param_is_ignored():
    db = _db()
    _limit_rule(db, rule_id="lr-t-ruim", max_dogs="dois")
    assert lrs.local_max_dogs_override(db, "Salvador", "BA") is None


def test_national_default_is_derived_from_shared_walk_config():
    config = TenantSharedWalkConfig(tenant_id="t1", max_tutors=2, max_pets_same_tutor=3)
    assert lrs.national_max_dogs_per_walker(config) == 6
