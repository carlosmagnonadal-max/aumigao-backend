"""
Suíte P2 — isolamento cross-tenant em Postgres real. Bloco 1: tabelas core.

Exercita as políticas RLS ativas (migration 0043) contra um Postgres de
verdade, onde o role aumigao_app tem NOBYPASSRLS.

Ativação: PG_TEST_DATABASE_URL deve estar definida (ver conftest.py).

Cobertura:
  - T01  Leitura tenant A não enxerga linhas do tenant B
  - T02  INSERT com tenant_id errado viola WITH CHECK (exception)
  - T03  Escopo global '*' enxerga linhas dos dois tenants
  - T04  GUC ausente / vazio = fail-closed (0 linhas)
"""
import psycopg2
import pytest

from tests.pg_rls.conftest import (
    _app_kwargs,
    app_session,
    make_uid,
    setup_pet,
    setup_tenants,
    setup_user,
)


class TestCoreIsolation:
    """T01-T04: pets (tabela core com tenant_id)."""

    def test_T01_read_isolation(self, owner_tx):
        """Tenant A não enxerga pets do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute("SELECT id FROM pets WHERE id = %s", (pb,))
                assert app_cur.fetchone() is None, (
                    "Tenant A conseguiu ler pet do Tenant B — RLS falhou"
                )

            with app_session(ta) as app_cur:
                app_cur.execute("SELECT id FROM pets WHERE id = %s", (pa,))
                assert app_cur.fetchone() is not None, (
                    "Tenant A não conseguiu ler seu próprio pet"
                )
        finally:
            # Limpar: owner deleta os dados inseridos
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T02_insert_wrong_tenant_violates_with_check(self, owner_tx):
        """INSERT de pet com tenant_id de outro tenant deve falhar com RLS."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        owner_tx.commit()

        pid = make_uid()
        try:
            with app_session(ta) as app_cur:
                with pytest.raises(psycopg2.errors.CheckViolation):
                    app_cur.execute(
                        """
                        INSERT INTO pets (id, tenant_id, tutor_user_id, name,
                                         species, breed, weight_kg, active,
                                         created_at, updated_at)
                        VALUES (%s, %s, %s, 'Ghost', 'dog', 'SRD', 3.0,
                                true, NOW(), NOW())
                        """,
                        (pid, tb, ua),  # tenant_id = tb mas sessão = ta
                    )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pets WHERE id = %s", (pid,))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T03_global_scope_sees_all(self, owner_tx):
        """Escopo '*' enxerga pets dos dois tenants."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)
        owner_tx.commit()

        try:
            with app_session("*") as app_cur:
                app_cur.execute(
                    "SELECT id FROM pets WHERE id IN (%s, %s)", (pa, pb)
                )
                found = {row[0] for row in app_cur.fetchall()}
                assert pa in found, "Pet A não visível no escopo global"
                assert pb in found, "Pet B não visível no escopo global"
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T04_empty_tenant_fail_closed(self, owner_tx):
        """GUC vazio/ausente = fail-closed (0 linhas visíveis para a app)."""
        cur = owner_tx.cursor()
        ta, _ = setup_tenants(cur)
        ua = setup_user(cur, ta)
        pa = setup_pet(cur, ta, ua)
        owner_tx.commit()

        try:
            # Conectar como app mas sem setar o GUC (permanece vazio).
            conn = psycopg2.connect(**_app_kwargs())
            conn.autocommit = False
            try:
                with conn.cursor() as app_cur:
                    # GUC não setado = string vazia = falha-fechada.
                    app_cur.execute("SELECT id FROM pets WHERE id = %s", (pa,))
                    assert app_cur.fetchone() is None, (
                        "Sem GUC definido, pet ainda visível — fail-closed falhou"
                    )
                conn.rollback()
            finally:
                conn.close()
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pets WHERE tenant_id = %s", (ta,))
            cur2.execute("DELETE FROM users WHERE tenant_id = %s", (ta,))
            cur2.execute("DELETE FROM tenants WHERE id = %s", (ta,))
            owner_tx.commit()
