"""
Fixtures compartilhadas para a suíte cross-tenant em Postgres real.

Ativação: exporte PG_TEST_DATABASE_URL apontando para um banco Postgres de
TESTE (não produção) antes de rodar. Sem essa variável todos os testes do
pacote são ignorados com pytest.skip limpo.

  export PG_TEST_DATABASE_URL="postgresql://aumigao_owner:senha@localhost:5432/aumigao_test"
  pytest tests/pg_rls/ -v

O que este conftest faz:
  1. Pula toda a sessão se PG_TEST_DATABASE_URL não estiver definida.
  2. Conecta como owner (SUPERUSER / CREATEROLE), cria o schema via
     Base.metadata.create_all e depois stampa o Alembic em head (sem
     rodar as migrations individualmente).  Por quê: a cadeia de migrations
     da casa é incremental (pressupõe schema pré-existente — padrão de
     produção); num banco vazio a migration 0002 falha porque 0001 é
     intencialmente NO-OP.  create_all cria o estado final das tabelas e
     o stamp avisa o Alembic que não há nada a fazer.  As policies RLS
     (que as migrations aplicam) são então reaplicadas via SQL idempotente
     diretamente sobre o schema já criado.
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

def _apply_rls_policies(sa_engine) -> None:
    """
    Aplica TODAS as policies RLS do projeto usando SQL idempotente.

    Chamado após create_all + stamp head, repõe o que as migrations de
    RLS (0043-0046, 0049, 0051, 0073-0077, 0080, 0081) fazem. Todo
    statement usa DROP POLICY IF EXISTS + CREATE POLICY / ALTER POLICY,
    portanto é seguro re-executar em banco já configurado.

    Recebe um SQLAlchemy engine (criado com PG_TEST_DATABASE_URL) para
    poder usar sa.inspect() na introspecção do schema.

    Não editamos as migrations antigas (padrão da casa); a lógica de
    bootstrap fica exclusivamente neste conftest.
    """
    import sqlalchemy as sa

    # -------------------------------------------------------------------------
    # Políticas padrão tenant_isolation (derived from 0043→0045).
    #
    # USING  : permite ler linhas sem tenant (anônimas/globais) + escopo tenant.
    # WITH CHECK : apenas sessões '*' podem gravar NULL (fecha buraco de escrita).
    # Exceções: upload_files e audit_logs mantêm WITH CHECK permissivo (pendência
    # de correção de app — idêntico ao estado atual das migrations 0045).
    # -------------------------------------------------------------------------
    _USING_PERMISSIVE = (
        "current_setting('app.current_tenant', true) = '*' "
        "OR tenant_id IS NULL "
        "OR tenant_id::text = current_setting('app.current_tenant', true)"
    )
    _WITH_CHECK_STRICT = (
        "current_setting('app.current_tenant', true) = '*' "
        "OR tenant_id::text = current_setting('app.current_tenant', true)"
    )
    _TABLES_PERMISSIVE_CHECK = {"upload_files", "audit_logs"}

    # execution_options deve ser aplicado no engine antes de abrir conexão (SA 2.x).
    ac_engine = sa_engine.execution_options(isolation_level="AUTOCOMMIT")
    with ac_engine.connect() as conn:
        insp = sa.inspect(sa_engine)
        all_tables = set(insp.get_table_names())

        # Tabelas com coluna tenant_id (recebem a policy padrão).
        for table in all_tables:
            if table == "alembic_version":
                continue
            cols = {c["name"] for c in insp.get_columns(table)}
            if "tenant_id" not in cols:
                continue

            with_check = (
                _USING_PERMISSIVE
                if table in _TABLES_PERMISSIVE_CHECK
                else _WITH_CHECK_STRICT
            )
            conn.execute(sa.text(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY'))
            conn.execute(sa.text(f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"'))
            conn.execute(sa.text(
                f'CREATE POLICY tenant_isolation ON "{table}" '
                f"USING ({_USING_PERMISSIVE}) "
                f"WITH CHECK ({with_check})"
            ))

        # -------------------------------------------------------------------------
        # walks: USING estendido com walker-self (migration 0049).
        # WITH CHECK permanece estrita (tenant-scope).
        # -------------------------------------------------------------------------
        if "walks" in all_tables:
            _WALKS_USING = """(
  current_setting('app.current_tenant', true) = '*'
  OR tenant_id::text = current_setting('app.current_tenant', true)
  OR (
    current_setting('app.current_user_id', true) NOT IN ('-', '')
    AND (
      walker_id::text = current_setting('app.current_user_id', true)
      OR assigned_walker_id::text = current_setting('app.current_user_id', true)
    )
  )
)"""
            _WALKS_WITH_CHECK = """(
  current_setting('app.current_tenant', true) = '*'
  OR tenant_id::text = current_setting('app.current_tenant', true)
)"""
            conn.execute(sa.text(
                f"ALTER POLICY tenant_isolation ON walks "
                f"USING {_WALKS_USING} "
                f"WITH CHECK {_WALKS_WITH_CHECK}"
            ))

        # -------------------------------------------------------------------------
        # webhook_events: sem tenant_id — acesso apenas por escopo global '*'
        # (migration 0080).
        # -------------------------------------------------------------------------
        if "webhook_events" in all_tables:
            _WEBHOOK_POLICY = "current_setting('app.current_tenant', true) = '*'"
            conn.execute(sa.text('ALTER TABLE "webhook_events" ENABLE ROW LEVEL SECURITY'))
            conn.execute(sa.text('DROP POLICY IF EXISTS tenant_isolation ON "webhook_events"'))
            conn.execute(sa.text(
                'CREATE POLICY tenant_isolation ON "webhook_events" '
                f"USING ({_WEBHOOK_POLICY}) WITH CHECK ({_WEBHOOK_POLICY})"
            ))


@pytest.fixture(scope="session", autouse=True)
def _pg_setup():
    """
    Prepara o banco de teste (schema + RLS policies) para a suíte.
    Cria o role aumigao_app se não existir.
    Garante GRANT de uso nas tabelas para o role da app.

    Estratégia de bootstrap (banco vazio):
      1. Base.metadata.create_all — cria TODAS as tabelas a partir dos modelos
         SQLAlchemy (estado final, sem depender da cadeia de migrations).
      2. alembic stamp head — registra o banco como já atualizado; evita que o
         Alembic tente re-aplicar migrations cujo DDL já foi executado pelo
         create_all.  Sem este stamp o próximo `alembic upgrade head` do CI
         tentaria re-criar tabelas e falharia.
      3. _apply_rls_policies — aplica as policies RLS via SQL idempotente,
         repondo o que as migrations 0043-0081 fariam.

    Por que NÃO usar `alembic upgrade head` direto num banco vazio:
      A migration 0001_baseline é intencionalmente NO-OP (schema já pré-existia
      em produção quando o Alembic foi introduzido).  A 0002 tenta ADD COLUMN em
      "payments", que não existe num banco novo → psycopg2.errors.UndefinedTable.
      Editando migrations antigas violaria o padrão da casa; a correção fica
      exclusivamente neste conftest.

    Roda UMA VEZ para a sessão inteira (session scope).
    Pula silenciosamente se PG_TEST_DATABASE_URL não estiver definida.
    """
    if not PG_TEST_DATABASE_URL:
        yield
        return

    import psycopg2  # importado apenas quando PG está disponível
    import sqlalchemy as sa

    env = os.environ.copy()
    env["DATABASE_URL"] = PG_TEST_DATABASE_URL

    # 1) Criar schema completo a partir dos modelos SQLAlchemy.
    #    Importamos app.models para garantir que TODOS os modelos estejam
    #    registrados em Base.metadata antes de chamar create_all.
    import app.models  # noqa: F401 — registra todos os modelos em Base.metadata
    from app.core.database import Base
    engine = sa.create_engine(PG_TEST_DATABASE_URL, poolclass=sa.pool.NullPool)
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        engine.dispose()
        pytest.fail(f"Base.metadata.create_all falhou: {exc}")

    # 2) Stampar o Alembic em head para que futuras execuções de `alembic upgrade
    #    head` no CI não tentem re-aplicar migrations sobre schema já criado.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "stamp", "head"],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        engine.dispose()
        pytest.fail(
            f"alembic stamp head falhou:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    # 3) Aplicar políticas RLS via SQL idempotente (substitui o que as migrations
    #    0043-0081 fazem em produção). Reutilizamos o engine criado acima para
    #    poder usar sa.inspect() na introspecção do schema.
    try:
        _apply_rls_policies(engine)
    except Exception as exc:
        engine.dispose()
        pytest.fail(f"_apply_rls_policies falhou: {exc}")
    finally:
        engine.dispose()

    # 4) Criar role da app + configurar permissões.
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
    """Insere dois tenants distintos e retorna (tenant_a_id, tenant_b_id).

    Alinhado ao modelo Tenant atual: usa 'status' (não 'active'), sem coluna
    active (removida em refactor que adicionou status).
    """
    ta, tb = make_uid(), make_uid()
    for tid in (ta, tb):
        cur.execute(
            """
            INSERT INTO tenants (id, slug, name, plan, status, created_at, updated_at)
            VALUES (%s, %s, %s, 'pro', 'active', NOW(), NOW())
            """,
            (tid, f"slug-{tid[:8]}", f"Tenant {tid[:8]}"),
        )
    return ta, tb


def setup_user(cur, tenant_id: str) -> str:
    """Insere um usuário pertencente ao tenant e retorna o user_id.

    Alinhado ao modelo User atual: password_hash (não hashed_password),
    full_name (não name), is_active (não active), sem cpf_encrypted.
    """
    uid = make_uid()
    cur.execute(
        """
        INSERT INTO users (id, tenant_id, email, password_hash, full_name,
                           role, is_active, created_at)
        VALUES (%s, %s, %s, 'hash', 'Test User', 'tutor', true, NOW())
        """,
        (uid, tenant_id, f"user-{uid[:8]}@test.com"),
    )
    return uid


def setup_pet(cur, tenant_id: str, user_id: str) -> str:
    """Insere um pet pertencente ao tenant e retorna o pet_id.

    Alinhado ao modelo Pet atual: tutor_id (não tutor_user_id), weight
    (não weight_kg), sem active, sem updated_at.
    """
    pid = make_uid()
    cur.execute(
        """
        INSERT INTO pets (id, tenant_id, tutor_id, name, species,
                          sex, breed, weight, created_at)
        VALUES (%s, %s, %s, 'Rex', 'dog', 'M', 'SRD', 5.0, NOW())
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
