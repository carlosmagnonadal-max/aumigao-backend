"""BG-6 — migration 0084: colunas de sancoes em walker_profiles.

Valida que:
- alembic resolve UM unico head e que a 0084 esta na cadeia (NAO fixa a 0084
  como head — migrations novas podem encadear por cima sem quebrar este teste);
- a revision id <= 32 chars;
- a 0084 encadeia na 0083_money_decimal (head anterior).
"""
from alembic.config import Config
from alembic.script import ScriptDirectory

_REV = "0084_walker_sanctions_check"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_and_0084_in_chain():
    script = _script()
    heads = list(script.get_heads())
    assert len(heads) == 1, heads
    chain = {rev.revision for rev in script.walk_revisions()}
    assert _REV in chain


def test_revision_id_within_32_chars():
    assert len(_REV) <= 32, len(_REV)


def test_0084_chains_on_0083():
    rev = _script().get_revision(_REV)
    assert rev.down_revision == "0083_money_decimal"
