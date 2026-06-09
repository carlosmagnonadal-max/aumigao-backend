"""Introspecção READ-ONLY do schema do banco.

Uso:
    python scripts/db_introspect.py [tabela ...]

Apenas LÊ o schema (colunas/índices/alembic_version) usando a DATABASE_URL do
projeto. NÃO altera nada. Existe para validar migrations sem precisar de comandos
ad-hoc `python -c` (que seriam amplos demais para liberar via permissão).
"""
import sys
from pathlib import Path

# Permite rodar o script direto (python scripts/db_introspect.py): adiciona a
# raiz do backend ao path para que o pacote `app` seja importável.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect

from app.core.database import _database_url, mask_database_url

DEFAULT_TABLES = [
    "payments",
    "walk_reviews",
    "walker_reviews",
    "walk_tips",
    "walk_completion_reviews",
    "complaints",
]


def main() -> None:
    url = _database_url()
    print("URL:", mask_database_url(url))
    insp = inspect(create_engine(url))
    all_tables = set(insp.get_table_names())
    print("total de tabelas:", len(all_tables))
    print("alembic_version presente?", "alembic_version" in all_tables)

    tables = sys.argv[1:] or DEFAULT_TABLES
    for t in tables:
        if t not in all_tables:
            print(f"  {t}: TABELA AUSENTE")
            continue
        cols = [c["name"] for c in insp.get_columns(t)]
        idx = [i["name"] for i in insp.get_indexes(t)]
        has_col = "tenant_id" in cols
        has_idx = f"ix_{t}_tenant_id" in idx
        print(
            f"  {t}: tenant_id="
            + ("OK" if has_col else "FALTA")
            + f" | ix_{t}_tenant_id="
            + ("OK" if has_idx else "FALTA")
        )


if __name__ == "__main__":
    main()
