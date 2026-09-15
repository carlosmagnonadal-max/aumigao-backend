"""RLS da tabela GLOBAL local_rules (migration 0111).

  - T-LR1  leitura liberada sob escopo de um tenant qualquer
  - T-LR2  escrita bloqueada sob escopo de tenant
  - T-LR3  escrita permitida no escopo global '*'
Pulado sem PG_TEST_DATABASE_URL (ver conftest).
"""
import psycopg2
import pytest

from tests.pg_rls.conftest import app_session, make_uid

_INSERT = """
    INSERT INTO local_rules (id, uf, municipio, nivel, tema, criterio, params_json, exigencia_json,
                             norma, confianca, status, confirmado_por, created_at, updated_at)
    VALUES (%s, 'BA', 'Salvador', 'municipal', 'focinheira', 'weight_min_kg', '{}', '{}',
            'Lei de teste', 'alta', 'vigente', 'plataforma', NOW(), NOW())
"""


class TestLocalRulesRLS:
    def test_LR1_read_allowed_under_tenant_scope(self, owner_tx):
        cur = owner_tx.cursor()
        rid = make_uid()
        cur.execute(_INSERT, (rid,))
        owner_tx.commit()
        try:
            with app_session(make_uid()) as app_cur:
                app_cur.execute("SELECT id FROM local_rules WHERE id = %s", (rid,))
                assert app_cur.fetchone() is not None, "local_rules deve ser legível sob escopo de tenant"
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM local_rules WHERE id = %s", (rid,))
            owner_tx.commit()

    def test_LR2_write_blocked_under_tenant_scope(self):
        with app_session(make_uid()) as app_cur:
            with pytest.raises((psycopg2.errors.InsufficientPrivilege, psycopg2.errors.CheckViolation)):
                app_cur.execute(_INSERT, (make_uid(),))

    def test_LR3_write_allowed_under_global_scope(self):
        rid = make_uid()
        with app_session("*") as app_cur:  # app_session faz rollback ao sair
            app_cur.execute(_INSERT, (rid,))
            app_cur.execute("SELECT id FROM local_rules WHERE id = %s", (rid,))
            assert app_cur.fetchone() is not None
