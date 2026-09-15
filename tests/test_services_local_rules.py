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
