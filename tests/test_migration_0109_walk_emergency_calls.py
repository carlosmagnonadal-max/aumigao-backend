"""S1 — migration 0109: walk_emergency_calls (RLS-ON).

Valida: 0109 na cadeia e encadeada na 0108; revision id <= 32 chars; ORM com as
colunas da spec; migration liga RLS com NULL allowance no USING e WITH CHECK.
(O teste de head único fica em test_migration_0107 — S2/S3 encadeiam 0110/0111.)
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

import app.models  # noqa: F401 - registra todas as tabelas no Base.metadata
from app.core.database import Base

_REV = "0109_walk_emergency_calls"
_MIGRATION = Path("alembic/versions/0109_walk_emergency_calls.py")


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_0109_in_chain_and_chains_on_0108():
    script = _script()
    assert _REV in {rev.revision for rev in script.walk_revisions()}
    assert script.get_revision(_REV).down_revision == "0108_user_apple_sub"


def test_revision_id_within_32_chars():
    assert len(_REV) <= 32


def test_orm_table_has_spec_columns():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("walk_emergency_calls")}
    assert {
        "id", "tenant_id", "walk_id", "walker_user_id", "tutor_user_id", "reason",
        "mode", "provider", "provider_call_id", "status", "fallback_reason",
        "created_at", "ended_at",
    } <= cols
    index_names = {ix["name"] for ix in insp.get_indexes("walk_emergency_calls")}
    assert {"ix_walk_emergency_calls_tenant_id", "ix_walk_emergency_calls_walk_created"} <= index_names


def test_migration_enables_rls_with_tenant_policy():
    source = _MIGRATION.read_text(encoding="utf-8")
    assert "ENABLE ROW LEVEL SECURITY" in source
    assert "CREATE POLICY tenant_isolation" in source
    assert "tenant_id IS NULL" in source
    assert "WITH CHECK" in source
