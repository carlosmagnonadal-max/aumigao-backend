"""S3 — migration 0111: local_rules (global) + pets.is_reactive/reactivity_notes.

Valida (padrão tests/test_migration_0107_walk_cancellation.py):
- modelos ORM refletem a tabela e as colunas novas;
- (Task 2) cadeia alembic, conteúdo do seed e upgrade idempotente.
"""
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base
from app.models.pet import Pet


def test_orm_has_local_rules_table_and_pet_columns():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)

    cols = {c["name"] for c in insp.get_columns("local_rules")}
    assert {
        "id", "uf", "municipio", "nivel", "tema", "criterio", "params_json",
        "exigencia_json", "norma", "fonte_url", "verificado_em", "confianca",
        "status", "confirmado_por", "created_at", "updated_at",
    } <= cols
    assert "tenant_id" not in cols  # tabela GLOBAL

    pet_cols = {c["name"] for c in insp.get_columns("pets")}
    assert {"is_reactive", "reactivity_notes"} <= pet_cols


def test_pet_is_reactive_defaults_false():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(Pet(id="p1", tutor_id="u1", name="Mel"))
    db.commit()
    pet = db.get(Pet, "p1")
    assert pet.is_reactive is False
    assert pet.reactivity_notes is None


import importlib.util
from datetime import date
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text

_REV = "0111_local_rules_pet_reactive"
_MIGRATION_PATH = Path(__file__).resolve().parents[1] / "alembic" / "versions" / f"{_REV}.py"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_0111_local_rules", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_head_and_0111_in_chain():
    script = _script()
    heads = list(script.get_heads())
    assert len(heads) == 1, heads
    assert _REV in {rev.revision for rev in script.walk_revisions()}


def test_revision_id_within_32_chars():
    assert len(_REV) <= 32, len(_REV)


def test_0111_chains_on_0110_walker_training():
    assert _script().get_revision(_REV).down_revision == "0110_walker_training"


def test_seed_only_high_confidence_facts_are_vigente():
    rows = _load_migration().SEED_ROWS
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))
    for r in rows:
        assert r["norma"].strip()
        assert r["fonte_url"].startswith("https://")
        assert r["verificado_em"] == date(2026, 9, 15)
        assert r["status"] in {"vigente", "conflito"}
        if r["status"] == "vigente":
            assert r["confianca"] in {"alta", "media"}
    # A11: nenhuma lei vigente de limite de cães — nada de limite_caes no seed.
    assert not [r for r in rows if r["tema"] == "limite_caes"]


def test_seed_salvador_beach_is_conflict_and_pe_rules_are_state_level():
    rows = {r["id"]: r for r in _load_migration().SEED_ROWS}
    praia = rows["lr-ba-salvador-praia"]
    assert praia["status"] == "conflito" and praia["confianca"] == "baixa"
    peso = rows["lr-ba-salvador-focinheira-peso"]
    assert peso["criterio"] == "weight_min_kg" and '"min_kg": 24' in peso["params_json"]
    assert '"inclusive": false' in peso["params_json"]  # lei: "acima de 24 kg" (estrito)
    assert rows["lr-ba-salvador-focinheira-reativo"]["criterio"] == "reactive"
    for rid in ("lr-pe-praia-coleira", "lr-pe-praia-dejetos"):
        assert rows[rid]["uf"] == "PE" and rows[rid]["municipio"] is None
        assert rows[rid]["nivel"] == "estadual"


def test_upgrade_creates_seeds_backfills_and_is_idempotent():
    mig = _load_migration()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Pet.__table__])
    with engine.begin() as conn:
        conn.execute(Pet.__table__.insert(), [
            {"id": "p-reativo", "tutor_id": "u1", "name": "Thor", "behavior_notes": "Calmo, Reativo"},
            {"id": "p-calmo", "tutor_id": "u1", "name": "Mel", "behavior_notes": "Calmo"},
        ])
    for _ in range(2):  # idempotência
        with engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                mig.upgrade()
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM local_rules")).scalar()
        flags = dict(conn.execute(text("SELECT id, is_reactive FROM pets")).all())
    assert total == len(mig.SEED_ROWS)
    assert bool(flags["p-reativo"]) is True
    assert bool(flags["p-calmo"]) is False
