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
