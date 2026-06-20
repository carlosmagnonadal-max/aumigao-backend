"""BG-1 — migration 0038 do Background Check.

Valida que:
- alembic resolve UM unico head e que ele e a 0038;
- a revision id <= 32 chars;
- a 0038 esta encadeada na 0037_user_token_version (down_revision).
"""
from alembic.config import Config
from alembic.script import ScriptDirectory


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_is_current():
    # Head avanca conforme novas migrations entram. Hoje: 0041 (encrypt CPF/RG,
    # encadeada na 0040). O importante e haver UM unico head (sem bifurcacao).
    script = _script()
    heads = list(script.get_heads())
    assert heads == ["0041_encrypt_cpf_rg"], heads


def test_revision_id_within_32_chars():
    rev = "0038_walker_background_check"
    assert len(rev) <= 32, len(rev)


def test_0038_chains_on_0037():
    script = _script()
    rev = script.get_revision("0038_walker_background_check")
    assert rev.down_revision == "0037_user_token_version"
