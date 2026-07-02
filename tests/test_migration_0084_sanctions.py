"""BG-6 — migration 0084: colunas de sancoes em walker_profiles.

Valida que:
- alembic resolve UM unico head e que ele e a 0084;
- a revision id <= 32 chars;
- a 0084 encadeia na 0083_money_decimal (head anterior).
"""
from alembic.config import Config
from alembic.script import ScriptDirectory

_REV = "0084_walker_sanctions_check"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_is_current():
    heads = list(_script().get_heads())
    assert len(heads) == 1, heads
    assert heads[0] == _REV


def test_revision_id_within_32_chars():
    assert len(_REV) <= 32, len(_REV)


def test_0084_chains_on_0083():
    rev = _script().get_revision(_REV)
    assert rev.down_revision == "0083_money_decimal"
