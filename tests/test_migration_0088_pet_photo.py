"""selfie obrigatoria — migration 0088: coluna pet_photo_url em walker_profiles.

Valida que:
- alembic resolve UM unico head e que a 0088 esta na cadeia (NAO fixa a 0088
  como head — migrations novas podem encadear por cima sem quebrar este teste);
- a revision id <= 32 chars;
- a 0088 encadeia na 0087_pet_self_walks (head anterior).
"""
from alembic.config import Config
from alembic.script import ScriptDirectory

_REV = "0088_walker_pet_photo_url"


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_and_0088_in_chain():
    script = _script()
    heads = list(script.get_heads())
    assert len(heads) == 1, heads
    chain = {rev.revision for rev in script.walk_revisions()}
    assert _REV in chain


def test_revision_id_within_32_chars():
    assert len(_REV) <= 32, len(_REV)


def test_0088_chains_on_0087():
    rev = _script().get_revision(_REV)
    assert rev.down_revision == "0087_pet_self_walks"
