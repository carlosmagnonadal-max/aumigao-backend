import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.core.feature_flags import multi_tenant_walker_enabled

ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT_DIR / ".env"

load_dotenv(ENV_PATH)


def _database_url() -> str:
    return (
        os.getenv("DATABASE_URL")
        or os.getenv("URL_DO_BANCO_DE_DADOS")
        or "sqlite:///./aumigao.db"
    ).strip().strip('"').strip("'")


SQLALCHEMY_DATABASE_URL = _database_url()


def get_database_diagnostics() -> dict[str, str]:
    diagnostics = {
        "database_url": SQLALCHEMY_DATABASE_URL,
        "env_path": str(ENV_PATH),
    }
    parsed = urlparse(SQLALCHEMY_DATABASE_URL)
    if parsed.scheme == "sqlite":
        raw_path = parsed.path or ""
        if SQLALCHEMY_DATABASE_URL.startswith("sqlite:///./"):
            sqlite_path = ROOT_DIR / SQLALCHEMY_DATABASE_URL.replace("sqlite:///./", "", 1)
        elif SQLALCHEMY_DATABASE_URL.startswith("sqlite:///"):
            sqlite_path = Path(raw_path)
        else:
            sqlite_path = ROOT_DIR / raw_path.lstrip("/")
        diagnostics["sqlite_path"] = str(sqlite_path.resolve())
    return diagnostics


def mask_database_url(database_url: str = SQLALCHEMY_DATABASE_URL) -> str:
    parsed = urlparse(database_url)
    if not parsed.password:
        return database_url
    return database_url.replace(f":{parsed.password}@", ":***@")


connect_args = {"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {}

# RLS GUCs use SET LOCAL (set_config(..., true)) at after_begin = transaction-scoped,
# compatible with pgbouncer transaction pooling; psycopg2 has no server-side prepared
# statements so it's pooler-safe.
#
# When the host contains "-pooler" (Neon pooled endpoint / pgbouncer in transaction
# mode), we skip client-side pooling entirely (NullPool) to avoid holding connections
# across pgbouncer transaction boundaries.  Without "-pooler" we keep the existing
# pool for direct connections.
_parsed_url = urlparse(SQLALCHEMY_DATABASE_URL)
_is_pooler_url = "-pooler" in (_parsed_url.hostname or "")

engine_kwargs: dict = {}
if not SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    if _is_pooler_url:
        engine_kwargs = {"poolclass": NullPool}
    else:
        engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 300, "pool_size": 5, "max_overflow": 10}
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args=connect_args, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Fase 2 — RLS: injeção do tenant na sessão via event listener.
#
# O listener after_begin é chamado a cada início de transação. Em SQLite
# (CI/testes) é NO-OP imediato — só age em PostgreSQL. Em PG, aplica o GUC
# app.current_tenant com escopo de transação (is_local=true), de forma
# parametrizada (sem interpolação de string).
#
# O valor lido vem de session.info["rls_tenant"]:
#   "*"  → super_admin / caller interno global (vê todas as linhas)
#   ""   → tenant não resolvido; fail-closed (policy retorna 0 linhas em PG)
#   str  → tenant_id específico
# ---------------------------------------------------------------------------
@event.listens_for(Session, "after_begin")
def _set_rls_tenant_on_begin(session: Session, transaction, connection) -> None:
    """Injeta app.current_tenant e app.current_user_id na transação PostgreSQL.

    NO-OP em qualquer dialeto diferente de postgresql (ex.: sqlite em testes).
    Parametrizado — jamais interpola strings no SQL.

    app.current_tenant:
      None → fail-closed (GUC vazio; policy bloqueia todas as linhas).
      '*'  → super_admin / caller interno global (vê tudo).
      str  → tenant_id específico.

    app.current_user_id:
      None / ausente → '-' (não autenticado / caller interno).
      str            → user.id do usuário autenticado.
    """
    if connection.dialect.name != "postgresql":
        return
    tenant = session.info.get("rls_tenant")
    conn_tenant = tenant if tenant is not None else ""
    connection.exec_driver_sql(
        "SELECT set_config('app.current_tenant', %s, true)",
        (conn_tenant,),
    )
    # app.current_user_id: default '-' para sessions sem usuário autenticado.
    user_id = session.info.get("rls_user_id", "-")
    try:
        connection.exec_driver_sql(
            "SELECT set_config('app.current_user_id', %s, true)",
            (user_id,),
        )
    except Exception:
        pass  # never break a transaction for user_id GUC bookkeeping


def set_session_tenant(db: Session, tenant: str) -> None:
    """Seta o tenant RLS na sessão de forma imediata e persistente na transação.

    Use para super_admin e act-as: garante que mudanças de tenant mid-request
    também sejam refletidas em transações já abertas.

    NO-OP em dialetos diferentes de postgresql (ex.: SQLite em testes).
    Em PostgreSQL aplica set_config para a transação corrente.
    """
    db.info["rls_tenant"] = tenant
    # Se já há uma transação ativa, reaplicar via SQL (o after_begin já passou).
    # Em SQLite (testes) pula o execute — set_config não existe no SQLite.
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": tenant})


def set_session_user(db: Session, user_id: str) -> None:
    """Seta o GUC app.current_user_id na transação corrente.

    Espelha set_session_tenant mas para o usuário autenticado.
    Chamado em get_current_user (auth.py) logo após validação do token.

    Valor padrão '-' indica "não autenticado / caller interno".
    NO-OP em SQLite (testes) — set_config não existe neste dialeto.
    Jamais propaga exceção: qualquer erro é silenciado para não bloquear requests.
    """
    db.info["rls_user_id"] = user_id
    bind = db.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    try:
        db.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": user_id})
    except Exception:
        pass  # never block a request for GUC bookkeeping


def get_global_db():
    """FastAPI dependency que fornece uma sessão do banco com escopo global (RLS irrestrito).

    Destinado a endpoints que precisam ver dados de TODOS os tenants sem restrição de
    tenant_id: webhooks de gateway de pagamento (Asaas, Efí), operações de plataforma.

    Diferença de get_db: não lê tenant_id de request.state — sempre usa "*".
    Isso permite que os testes sobrescrevam via dependency_overrides, exatamente como
    fariam com get_db (sem depender de global_scope_session interno).
    """
    db = SessionLocal()
    db.info["rls_tenant"] = "*"
    try:
        yield db
    finally:
        db.close()


def get_db(request: Request = None):  # type: ignore[assignment]
    """FastAPI dependency que fornece uma sessão do banco com tenant RLS injetado.

    Quando chamado pelo FastAPI (request != None): lê tenant_id de request.state.
    Quando chamado diretamente sem request (callers internos/testes): usa "*"
    (acesso global), pois callers diretos são operações de plataforma.
    """
    db = SessionLocal()
    if request is not None:
        tenant_id = getattr(getattr(request, "state", None), "tenant_id", None)
        db.info["rls_tenant"] = tenant_id or ""
    else:
        # Caller interno ou teste: acesso global (sem RLS restritivo).
        db.info["rls_tenant"] = "*"
    try:
        yield db
    finally:
        db.close()


def get_walker_self_db(request: Request = None):  # type: ignore[assignment]
    """Sessão de leitura "minha" do passeador (Fase 1, Passo 2).

    Flag ON  → escopo global RLS (rls_tenant="*"); a QUERY DA ROTA *precisa*
               filtrar walker_id==user.id — o RLS não isola por walker neste modo.
    Flag OFF → idêntico a get_db (tenant-scoped) → zero-regressão.

    app.current_user_id é setado por get_current_user/set_session_user (auth.py),
    que é chamado antes desta dependency nas rotas.
    """
    db = SessionLocal()
    if multi_tenant_walker_enabled():
        db.info["rls_tenant"] = "*"
    else:
        tenant_id = getattr(getattr(request, "state", None), "tenant_id", None)
        db.info["rls_tenant"] = tenant_id or ""
    try:
        yield db
    finally:
        db.close()
