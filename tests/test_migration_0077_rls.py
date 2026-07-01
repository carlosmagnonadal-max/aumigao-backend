"""P0-1 — migration 0077 que habilita RLS nas 3 tabelas de growth loops.

Valida que:
- alembic resolve UM unico head (sem bifurcacao);
- a revision id <= 32 chars;
- a 0077 esta encadeada na 0076_pet_share_links (down_revision);
- o SQL do upgrade habilita ROW LEVEL SECURITY nas 3 tabelas
  (walk_share_links, tutor_referral_configs, tutor_referrals).

A suite roda em SQLite (RLS e no-op), entao NAO testamos RLS em runtime —
apenas o encadeamento + o conteudo do arquivo de migration.
"""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

REVISION = "0077_rls_growth_loops_tables"
DOWN_REVISION = "0076_pet_share_links"
RLS_TABLES = ("walk_share_links", "tutor_referral_configs", "tutor_referrals")

_MIGRATION_FILE = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "0077_rls_growth_loops_tables.py"
)


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_single_head_is_current():
    # Head avanca conforme novas migrations entram; o importante e haver UM unico head.
    script = _script()
    heads = list(script.get_heads())
    assert len(heads) == 1, heads


def test_revision_id_within_32_chars():
    assert len(REVISION) <= 32, len(REVISION)


def test_0077_chains_on_0076():
    script = _script()
    rev = script.get_revision(REVISION)
    assert rev.down_revision == DOWN_REVISION


def test_head_is_0077():
    script = _script()
    heads = list(script.get_heads())
    assert heads == [REVISION], heads


def test_upgrade_enables_rls_on_all_three_tables():
    text = _MIGRATION_FILE.read_text(encoding="utf-8")
    for table in RLS_TABLES:
        assert table in text, table
    # Deve conter o comando de habilitar RLS (padrao _enable_rls das 0073-0076).
    assert "ENABLE ROW LEVEL SECURITY" in text
    # Uma habilitacao por tabela (via _enable_rls chamado 3x).
    for table in RLS_TABLES:
        assert f'"{table}"' in text


def test_downgrade_disables_rls():
    text = _MIGRATION_FILE.read_text(encoding="utf-8")
    assert "DISABLE ROW LEVEL SECURITY" in text
    assert "DROP POLICY" in text
