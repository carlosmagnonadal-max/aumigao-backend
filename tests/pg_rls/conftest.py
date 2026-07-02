"""
Fixtures compartilhadas para a suíte cross-tenant em Postgres real.

Ativação: exporte PG_TEST_DATABASE_URL apontando para um banco Postgres de
TESTE (não produção) antes de rodar. Sem essa variável todos os testes do
pacote são ignorados com pytest.skip limpo.

  export PG_TEST_DATABASE_URL="postgresql://aumigao_owner:senha@localhost:5432/aumigao_test"
  pytest tests/pg_rls/ -v

O que este conftest faz:
  1. Pula toda a sessão se PG_TEST_DATABASE_URL não estiver definida.
  2. Conecta como owner (SUPERUSER / CREATEROLE), aplica todas as migrations
     Alembic (schema + RLS) em session scope.
  3. Garante que o role da app (aumigao_app) existe — non-owner, sem
     BYPASSRLS — espelho exato do role de produção.
  4. Expõe fixtures de sessão SQL via psycopg2 puro (sem ORM), que é o
     nível mais confiável para testar políticas RLS:
       - pg_owner_conn  → conexão como owner (bypass RLS para setup)
       - owner_tx       → transação isolada por teste (rollback ao final)
  5. Cada teste recebe conexões em transação isolada, com rollback ao final
     (sem sujar o banco entre testes).
"""
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import pytest

# ---------------------------------------------------------------------------
# Guard: pula TODA a suíte se a env var não estiver definida.
# Usa o hook pytest_configure para registrar o skip antes de qualquer
# coleta — essa é a forma aprovada pelo pytest para skip em conftest.
# ---------------------------------------------------------------------------
PG_TEST_DATABASE_URL = os.getenv("PG_TEST_DATABASE_URL", "")

# Raiz do worktree (dois níveis acima de tests/pg_rls/)
_ROOT = Path(__file__).resolve().parents[2]

# Role que a app usa em produção (non-owner, no BYPASSRLS).
APP_ROLE = "aumigao_app"
APP_ROLE_PASSWORD = "aumigao_app_test_pw"


def pytest_collection_modifyitems(config, items):
    """Marca todos os itens deste pacote com skip se PG_TEST_DATABASE_URL ausente."""
    if PG_TEST_DATABASE_URL:
        return
    skip_marker = pytest.mark.skip(
        reason=(
            "PG_TEST_DATABASE_URL não definida — suíte cross-tenant PG ignorada. "
            "Defina a variável apontando para um banco Postgres de TESTE para rodar."
        )
    )
    for item in items:
        # Só marcar itens dentro deste pacote (tests/pg_rls/).
        if "pg_rls" in str(item.fspath):
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Utilitários de conexão psycopg2 (URL → dict de kwargs)
# ---------------------------------------------------------------------------

def _parse_pg_url(url: str) -> dict:
    """Converte postgresql://user:pass@host:port/dbname em kwargs do psycopg2."""
    p = urlparse(url)
    return {
        "host": p.hostname or "localhost",
        "port": p.port or 5432,
        "dbname": p.path.lstrip("/"),
        "user": p.username,
        "password": p.password,
    }


def _owner_kwargs() -> dict:
    return _parse_pg_url(PG_TEST_DATABASE_URL) if PG_TEST_DATABASE_URL else {}


def _app_kwargs() -> dict:
    """Kwargs para conectar como aumigao_app (role da app sem BYPASSRLS)."""
    base = _owner_kwargs()
    return {**base, "user": APP_ROLE, "password": APP_ROLE_PASSWORD}


# ---------------------------------------------------------------------------
# Session-scoped: aplica migrations + cria role da app UMA VEZ por sessão.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _pg_setup():
    """
    Aplica Alembic upgrade head no banco de teste (schema + RLS policies).
    Cria o role aumigao_app se não existir.
    Garante GRANT de uso nas tabelas para o role da app.

    Roda UMA VEZ para a sessão inteira (session scope).
    Pula silenciosamente se PG_TEST_DATABASE_URL não estiver definida.
    """
    if not PG_TEST_DATABASE_URL:
        yield
        return

    import psycopg2  # importado apenas quando PG está disponível

    # 1) Aplicar migrations via Alembic (DATABASE_URL apontando pro PG de teste).
    env = os.environ.copy()
    env["DATABASE_URL"] = PG_TEST_DATABASE_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"alembic upgrade head falhou:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    # 2) Criar role da app + configurar permissões.
    with psycopg2.connect(**_owner_kwargs()) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            # Criar role se não existir.
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,)
            )
            if not cur.fetchone():
                cur.execute(
                    f"CREATE ROLE {APP_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB "
                    f"NOCREATEROLE NOINHERIT PASSWORD %s",
                    (APP_ROLE_PASSWORD,),
                )

            # Garantir NOINHERIT e NOBYPASSRLS (produção).
            cur.execute(f"ALTER ROLE {APP_ROLE} NOINHERIT NOBYPASSRLS")
            cur.execute(f"ALTER ROLE {APP_ROLE} PASSWORD %s", (APP_ROLE_PASSWORD,))

            # GRANT de uso no schema + SELECT/INSERT/UPDATE/DELETE nas tabelas.
            cur.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
            cur.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
            )
            cur.execute(
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"
            )

    yield  # testes rodam aqui

    # Teardown: não dropar o schema — banco de teste é reutilizável.


# ---------------------------------------------------------------------------
# Fixture: conexão owner (session) — usada para inserts de setup nos testes.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_owner_conn():
    """Conexão psycopg2 como owner do banco (bypass RLS). Session-scoped."""
    if not PG_TEST_DATABASE_URL:
        pytest.skip("PG_TEST_DATABASE_URL não definida")
    import psycopg2
    conn = psycopg2.connect(**_owner_kwargs())
    conn.autocommit = False
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Fixture: transação isolada por teste (rollback ao final).
# ---------------------------------------------------------------------------

@pytest.fixture()
def owner_tx(pg_owner_conn):
    """
    Transação isolada como owner para setup de dados por teste.
    Faz ROLLBACK ao final — banco sempre limpo entre testes.

    Nota: os testes fazem commit explícito para que os dados sejam visíveis
    para conexões externas (como a conexão do role aumigao_app). O rollback
    final garante limpeza, mas o cleanup é feito pelos próprios testes.
    """
    pg_owner_conn.autocommit = False
    yield pg_owner_conn
    pg_owner_conn.rollback()


# ---------------------------------------------------------------------------
# Utilitário: conexão como aumigao_app com tenant configurado via GUC.
# Exportado para uso nos testes.
# ---------------------------------------------------------------------------

@contextmanager
def app_session(tenant: str):
    """
    Context manager que abre uma conexão como aumigao_app e seta
    app.current_tenant para o tenant informado ('*' = global).

    Exemplo:
        with app_session("tenant_a") as cur:
            cur.execute("SELECT id FROM pets")
            rows = cur.fetchall()
    """
    import psycopg2
    conn = psycopg2.connect(**_app_kwargs())
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                (tenant,),
            )
            yield cur
        conn.rollback()  # nunca persistir em app_session — somente leitura/verificação
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers de setup de dados compartilhados entre os blocos de teste.
# ---------------------------------------------------------------------------

def make_uid() -> str:
    """Retorna um UUID string para IDs de teste."""
    import uuid
    return str(uuid.uuid4())


def setup_tenants(cur) -> tuple[str, str]:
    """Insere dois tenants distintos e retorna (tenant_a_id, tenant_b_id)."""
    ta, tb = make_uid(), make_uid()
    for tid in (ta, tb):
        cur.execute(
            """
            INSERT INTO tenants (id, slug, name, plan, active, created_at, updated_at)
            VALUES (%s, %s, %s, 'pro', true, NOW(), NOW())
            """,
            (tid, f"slug-{tid[:8]}", f"Tenant {tid[:8]}"),
        )
    return ta, tb


def setup_user(cur, tenant_id: str) -> str:
    """Insere um usuário pertencente ao tenant e retorna o user_id."""
    uid = make_uid()
    cur.execute(
        """
        INSERT INTO users (id, tenant_id, email, hashed_password, name,
                           cpf_encrypted, role, active, created_at, updated_at)
        VALUES (%s, %s, %s, 'hash', 'Test User', 'enc', 'tutor', true, NOW(), NOW())
        """,
        (uid, tenant_id, f"user-{uid[:8]}@test.com"),
    )
    return uid


def setup_pet(cur, tenant_id: str, user_id: str) -> str:
    """Insere um pet pertencente ao tenant e retorna o pet_id."""
    pid = make_uid()
    cur.execute(
        """
        INSERT INTO pets (id, tenant_id, tutor_user_id, name, species,
                          breed, weight_kg, active, created_at, updated_at)
        VALUES (%s, %s, %s, 'Rex', 'dog', 'SRD', 5.0, true, NOW(), NOW())
        """,
        (pid, tenant_id, user_id),
    )
    return pid


__all__ = [
    "app_session",
    "APP_ROLE",
    "PG_TEST_DATABASE_URL",
    "make_uid",
    "setup_tenants",
    "setup_user",
    "setup_pet",
]
