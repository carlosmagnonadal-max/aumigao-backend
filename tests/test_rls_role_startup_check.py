"""
Testes da rede de segurança que valida, no boot, que o role Postgres de
runtime nao tem bypass de RLS (BYPASSRLS/SUPERUSER).

Cobre a funcao de decisao pura (_evaluate_rls_role_row) com mocks simples
(sem banco real) e o no-op de assert_rls_enforced_role em dialeto sqlite
(usando o engine sqlite da propria suite de testes).
"""
import pytest

from app.core.database import (
    RLSRoleMisconfiguredError,
    _evaluate_rls_role_row,
    assert_rls_enforced_role,
    engine,
)


def test_evaluate_rls_role_row_passes_when_no_bypass():
    """rolbypassrls=False e rolsuper=False -> nao levanta excecao."""
    _evaluate_rls_role_row(("aumigao_app", False, False))


def test_evaluate_rls_role_row_raises_on_bypassrls():
    with pytest.raises(RLSRoleMisconfiguredError):
        _evaluate_rls_role_row(("neondb_owner", True, False))


def test_evaluate_rls_role_row_raises_on_superuser():
    with pytest.raises(RLSRoleMisconfiguredError):
        _evaluate_rls_role_row(("postgres", False, True))


def test_evaluate_rls_role_row_raises_on_both():
    with pytest.raises(RLSRoleMisconfiguredError):
        _evaluate_rls_role_row(("postgres", True, True))


def test_evaluate_rls_role_row_raises_when_role_not_found():
    """Row None (role nao encontrado em pg_roles) -> fail-closed."""
    with pytest.raises(RLSRoleMisconfiguredError):
        _evaluate_rls_role_row(None)


def test_evaluate_rls_role_row_error_message_no_connection_string_leak():
    """A mensagem de erro nao deve vazar a connection string (usuario/senha/host)."""
    with pytest.raises(RLSRoleMisconfiguredError) as exc_info:
        _evaluate_rls_role_row(("neondb_owner", True, False))
    message = str(exc_info.value)
    assert "://" not in message
    assert "@" not in message


def test_assert_rls_enforced_role_noop_on_sqlite():
    """Em dialeto sqlite (o engine de teste da suite), a checagem e NO-OP:
    nao levanta excecao mesmo sem nenhuma tabela pg_roles existir."""
    assert engine.dialect.name == "sqlite"
    assert_rls_enforced_role(engine)  # nao deve levantar nada


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConnection:
    """Simula sqlalchemy.engine.Connection o suficiente para exercitar
    assert_rls_enforced_role sem abrir conexao real com Postgres."""

    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, clause, *args, **kwargs):
        self.executed.append(str(clause))
        return _FakeResult(self._row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeTransactionEngine:
    """Simula um Engine cujo dialect.name == 'postgresql' e cujo .begin()
    retorna uma conexao fake que devolve a linha configurada."""

    class _Dialect:
        name = "postgresql"

    def __init__(self, row):
        self.dialect = self._Dialect()
        self._row = row

    def begin(self):
        return _FakeConnection(self._row)


def test_assert_rls_enforced_role_raises_for_postgresql_bypass_role():
    fake_engine = _FakeTransactionEngine(("neondb_owner", True, False))
    with pytest.raises(RLSRoleMisconfiguredError):
        assert_rls_enforced_role(fake_engine)


def test_assert_rls_enforced_role_passes_for_postgresql_safe_role():
    fake_engine = _FakeTransactionEngine(("aumigao_app", False, False))
    assert_rls_enforced_role(fake_engine)  # nao deve levantar nada
