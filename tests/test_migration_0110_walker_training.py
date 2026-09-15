"""S2 — migration 0110: walker_training_progress + walker_profiles.training_completed_*.

Tabela GLOBAL (sem tenant_id / sem RLS), igual a walker_profiles e walker_kit_submissions.
"""
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.core.database import Base
from app.models.walker_training_progress import WalkerTrainingProgress

_REV = "0110_walker_training"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_and_0110_in_chain():
    script = _script()
    assert len(list(script.get_heads())) == 1
    assert _REV in {rev.revision for rev in script.walk_revisions()}


def test_revision_id_within_32_chars():
    assert len(_REV) <= 32


def test_0110_chains_on_0109():
    assert _script().get_revision(_REV).down_revision == "0109_walk_emergency_calls"


def test_orm_has_table_and_profile_columns():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("walker_training_progress")}
    assert {"id", "walker_user_id", "content_version", "module_id", "best_score", "attempts",
            "passed_at", "created_at", "updated_at"} <= cols
    assert "tenant_id" not in cols
    profile_cols = {c["name"] for c in insp.get_columns("walker_profiles")}
    assert {"training_completed_version", "training_completed_at"} <= profile_cols


def test_unique_progress_per_walker_version_module():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(WalkerTrainingProgress(id="p1", walker_user_id="w", content_version="1.0", module_id="m01"))
    db.commit()
    db.add(WalkerTrainingProgress(id="p2", walker_user_id="w", content_version="1.0", module_id="m01"))
    with pytest.raises(IntegrityError):
        db.commit()
