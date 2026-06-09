"""Introspecção READ-ONLY do schema/dados do banco.

Uso:
    python scripts/db_introspect.py [tabela ...]

Apenas LÊ (colunas, índices, alembic_version e contagem de tenant_id preenchido)
usando a DATABASE_URL do projeto. NÃO altera nada. Existe para validar migrations
sem precisar de comandos ad-hoc `python -c` (amplos demais para liberar via permissão).
"""
import sys
from pathlib import Path

# Permite rodar o script direto (python scripts/db_introspect.py): adiciona a
# raiz do backend ao path para que o pacote `app` seja importável.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, inspect, text

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
    engine = create_engine(url)
    insp = inspect(engine)
    all_tables = set(insp.get_table_names())
    print("total de tabelas:", len(all_tables))
    print("alembic_version presente?", "alembic_version" in all_tables)

    tables = sys.argv[1:] or DEFAULT_TABLES
    with engine.connect() as conn:
        if "rbac" in sys.argv[1:]:
            print("Permissoes por papel:")
            for r in conn.execute(
                text(
                    "SELECT ro.name, COUNT(rp.permission_id) AS perms "
                    "FROM roles ro LEFT JOIN role_permissions rp ON rp.role_id = ro.id "
                    "GROUP BY ro.name ORDER BY perms DESC"
                )
            ):
                print(f"  {r[0]}: {r[1]} permissoes")
            print("Atribuicoes ativas por papel:")
            for r in conn.execute(
                text(
                    "SELECT ro.name, COUNT(*) AS c "
                    "FROM user_role_assignments ura JOIN roles ro ON ro.id = ura.role_id "
                    "WHERE ura.revoked_at IS NULL GROUP BY ro.name ORDER BY c DESC"
                )
            ):
                print(f"  {r[0]}: {r[1]} usuarios")
            # Sanity: quantos usuarios tem uma permissao via RBAC (mesma query do
            # user_has_permission). Confirma que ninguem perde acesso ao migrar.
            for key in ("finance.read", "walks.update_status"):
                n = conn.execute(
                    text(
                        "SELECT COUNT(DISTINCT ura.user_id) FROM user_role_assignments ura "
                        "JOIN role_permissions rp ON rp.role_id = ura.role_id "
                        "JOIN permissions p ON p.id = rp.permission_id "
                        "WHERE ura.revoked_at IS NULL AND p.key = :k"
                    ),
                    {"k": key},
                ).scalar()
                print(f"usuarios com {key} (via RBAC, sem contar bypass super_admin): {n}")
            return
        if "user-roles" in sys.argv[1:]:
            print("Distribuicao de users.role:")
            for r in conn.execute(
                text("SELECT role, COUNT(*) AS c FROM users GROUP BY role ORDER BY c DESC")
            ):
                print(f"  {r[0]!r}: {r[1]}")
            return
        for t in tables:
            if t not in all_tables:
                print(f"  {t}: TABELA AUSENTE")
                continue
            cols = [c["name"] for c in insp.get_columns(t)]
            idx = [i["name"] for i in insp.get_indexes(t)]
            has_col = "tenant_id" in cols
            has_idx = f"ix_{t}_tenant_id" in idx
            line = (
                f"  {t}: tenant_id="
                + ("OK" if has_col else "FALTA")
                + f" | ix_{t}_tenant_id="
                + ("OK" if has_idx else "FALTA")
            )
            if has_col:
                total = conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                filled = conn.execute(
                    text(f"SELECT COUNT(*) FROM {t} WHERE tenant_id IS NOT NULL")
                ).scalar()
                line += f" | preenchidos={filled}/{total}"
            print(line)


if __name__ == "__main__":
    main()
