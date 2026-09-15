"""
Suíte P2 — isolamento cross-tenant em Postgres real: walk_emergency_calls (0109).

Ativação: PG_TEST_DATABASE_URL deve estar definida (ver conftest.py); sem ela o
pacote inteiro é pulado. A policy padrão é aplicada automaticamente pelo conftest
(introspecção de tabelas com tenant_id).

Cobertura:
  - T22  walk_emergency_calls isolados por tenant
  - T23  walk_emergency_calls WITH CHECK rejeita tenant errado
"""
import psycopg2
import pytest

from tests.pg_rls.conftest import (
    app_session,
    make_uid,
    setup_pet,
    setup_tenants,
    setup_user,
)


def _insert_walk(cur, wid, tid, uid, pid):
    cur.execute(
        """
        INSERT INTO walks (id, tenant_id, tutor_id, pet_id, walker_id,
                           status, scheduled_date, duration_minutes, price,
                           pickup_method, modality, destination,
                           address_snapshot, notes, operational_status,
                           walker_selection_mode, current_attempt,
                           max_attempts, credit_refunded, is_referral_gift,
                           created_at)
        VALUES (%s, %s, %s, %s, %s,
                'Passeando agora', '2026-01-01', 30, 0.0,
                'Buscar em casa', 'standard', '', '', '',
                'ride_in_progress', 'auto', 0, 3, false, false,
                NOW())
        """,
        (wid, tid, uid, pid, uid),
    )


class TestWalkEmergencyCallsRls:
    def test_T22_walk_emergency_calls_isolated(self, owner_tx):
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)
        wa, wb = make_uid(), make_uid()
        _insert_walk(cur, wa, ta, ua, pa)
        _insert_walk(cur, wb, tb, ub, pb)
        ca, cb = make_uid(), make_uid()
        for cid, wid, tid, uid in [(ca, wa, ta, ua), (cb, wb, tb, ub)]:
            cur.execute(
                """
                INSERT INTO walk_emergency_calls
                    (id, tenant_id, walk_id, walker_user_id, tutor_user_id,
                     mode, provider, status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'direct', 'none', 'initiated', NOW())
                """,
                (cid, tid, wid, uid, uid),
            )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute("SELECT id FROM walk_emergency_calls WHERE id = %s", (cb,))
                assert app_cur.fetchone() is None, "Tenant A enxerga emergência do tenant B"
            with app_session(ta) as app_cur:
                app_cur.execute("SELECT id FROM walk_emergency_calls WHERE id = %s", (ca,))
                assert app_cur.fetchone() is not None, "Tenant A não enxerga a própria emergência"
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM walk_emergency_calls WHERE id IN (%s, %s)", (ca, cb))
            cur2.execute("DELETE FROM walks WHERE id IN (%s, %s)", (wa, wb))
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T23_walk_emergency_calls_with_check(self, owner_tx):
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        pa = setup_pet(cur, ta, ua)
        wa = make_uid()
        _insert_walk(cur, wa, ta, ua, pa)
        owner_tx.commit()

        cid = make_uid()
        try:
            with app_session(ta) as app_cur:
                with pytest.raises((psycopg2.errors.CheckViolation,
                                    psycopg2.errors.InsufficientPrivilege)):
                    app_cur.execute(
                        """
                        INSERT INTO walk_emergency_calls
                            (id, tenant_id, walk_id, walker_user_id, tutor_user_id,
                             mode, provider, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, 'direct', 'none', 'initiated', NOW())
                        """,
                        (cid, tb, wa, ua, ua),  # sessão=ta mas tenant_id=tb
                    )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM walk_emergency_calls WHERE id = %s", (cid,))
            cur2.execute("DELETE FROM walks WHERE id = %s", (wa,))
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()
