"""
Testes da trava de segurança da Onda 0 (test-T1 / rastreabilidade C14).

Garante que a suíte só roda contra banco LOCAL e que o detector de banco
remoto/produção funciona — protege contra alguém reintroduzir acesso a prod.
"""
import conftest


def test_guard_aceita_sqlite_local():
    assert conftest._is_local_sqlite("sqlite:///./test.db") is True
    assert conftest._is_local_sqlite("sqlite:///:memory:") is True


def test_guard_aceita_localhost():
    assert conftest._is_local_sqlite("postgresql://u:p@localhost:5432/db") is True
    assert conftest._is_local_sqlite("postgresql://u:p@127.0.0.1/db") is True


def test_guard_rejeita_banco_remoto_de_producao():
    assert conftest._is_local_sqlite("postgresql://u:p@ep-x-123.sa-east-1.aws.neon.tech/db") is False
    assert conftest._is_local_sqlite("postgresql://u:p@host.rds.amazonaws.com/db") is False


def test_suite_resolveu_banco_local():
    """Com a trava ativa, o app DEVE ter resolvido um SQLite local."""
    from app.core.database import SQLALCHEMY_DATABASE_URL

    assert conftest._is_local_sqlite(SQLALCHEMY_DATABASE_URL), (
        f"Suite resolveu banco NAO-local: {SQLALCHEMY_DATABASE_URL.split('@')[-1]}"
    )
