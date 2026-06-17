"""BG-1 — migration 0035 do Background Check.

Valida que:
- alembic resolve UM unico head e que ele e a 0035;
- a revision id <= 32 chars;
- a 0035 esta encadeada na 0034 (down_revision).
"""
from alembic.config import Config
from alembic.script import ScriptDirectory


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_is_0035():
    script = _script()
    heads = list(script.get_heads())
    assert heads == ["0035_walker_background_check"], heads


def test_revision_id_within_32_chars():
    rev = "0035_walker_background_check"
    assert len(rev) <= 32, len(rev)


def test_0035_chains_on_0034():
    script = _script()
    rev = script.get_revision("0035_walker_background_check")
    assert rev.down_revision == "0034_walker_max_dog_size"
