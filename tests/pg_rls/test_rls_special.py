"""
Suíte P2 — isolamento cross-tenant em Postgres real. Bloco 4: comportamentos especiais.

NULL allowance das policies (0073-0077) e isolamento de GUC entre conexões.

Ativação: PG_TEST_DATABASE_URL deve estar definida (ver conftest.py).

Cobertura:
  - T17  NULL tenant_id em pet_timeline_events visível no escopo global
  - T18  NULL tenant_id em pet_timeline_events visível no escopo do tenant dono
  - T19  Troca de tenant entre transações/conexões (SET LOCAL não vaza)
"""
from tests.pg_rls.conftest import (
    app_session,
    make_uid,
    setup_pet,
    setup_tenants,
    setup_user,
)


class TestSpecialBehaviors:
    """T17-T19: NULL tenant_id, troca de tenant entre conexões."""

    def test_T17_null_tenant_visible_global_scope(self, owner_tx):
        """Evento com tenant_id=NULL é visível no escopo global '*'."""
        cur = owner_tx.cursor()
        ta, _ = setup_tenants(cur)
        ua = setup_user(cur, ta)
        pa = setup_pet(cur, ta, ua)

        eid = make_uid()
        cur.execute(
            """
            INSERT INTO pet_timeline_events
                (id, pet_id, tenant_id, event_type, title, notes,
                 source, occurred_at, created_at)
            VALUES (%s, %s, NULL, 'system', 'Evento Global', '',
                    'system', NOW(), NOW())
            """,
            (eid, pa),
        )
        owner_tx.commit()

        try:
            with app_session("*") as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_timeline_events WHERE id = %s", (eid,)
                )
                assert app_cur.fetchone() is not None, (
                    "Evento com tenant_id=NULL não visível no escopo global"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pet_timeline_events WHERE id = %s", (eid,))
            cur2.execute("DELETE FROM pets WHERE tenant_id = %s", (ta,))
            cur2.execute("DELETE FROM users WHERE tenant_id = %s", (ta,))
            cur2.execute("DELETE FROM tenants WHERE id = %s", (ta,))
            owner_tx.commit()

    def test_T18_null_tenant_visible_to_tenant_scope(self, owner_tx):
        """Evento com tenant_id=NULL é visível para qualquer tenant
        (NULL allowance nas policies 0073-0077)."""
        cur = owner_tx.cursor()
        ta, _ = setup_tenants(cur)
        ua = setup_user(cur, ta)
        pa = setup_pet(cur, ta, ua)

        eid = make_uid()
        cur.execute(
            """
            INSERT INTO pet_timeline_events
                (id, pet_id, tenant_id, event_type, title, notes,
                 source, occurred_at, created_at)
            VALUES (%s, %s, NULL, 'system', 'Evento Global', '',
                    'system', NOW(), NOW())
            """,
            (eid, pa),
        )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_timeline_events WHERE id = %s", (eid,)
                )
                assert app_cur.fetchone() is not None, (
                    "Evento com tenant_id=NULL não visível para tenant dono do pet"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pet_timeline_events WHERE id = %s", (eid,))
            cur2.execute("DELETE FROM pets WHERE tenant_id = %s", (ta,))
            cur2.execute("DELETE FROM users WHERE tenant_id = %s", (ta,))
            cur2.execute("DELETE FROM tenants WHERE id = %s", (ta,))
            owner_tx.commit()

    def test_T19_tenant_switch_between_transactions(self, owner_tx):
        """Troca de tenant em transações distintas é efetiva (SET LOCAL).

        Abre duas conexões separadas: uma como ta, outra como tb.
        Garante que o GUC de uma não vaza para a outra.
        """
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)
        owner_tx.commit()

        try:
            # Conexão 1 como ta: enxerga pa, não enxerga pb.
            with app_session(ta) as app_cur_a:
                app_cur_a.execute("SELECT id FROM pets WHERE id = %s", (pa,))
                assert app_cur_a.fetchone() is not None

                app_cur_a.execute("SELECT id FROM pets WHERE id = %s", (pb,))
                assert app_cur_a.fetchone() is None

            # Conexão 2 como tb: enxerga pb, não enxerga pa.
            with app_session(tb) as app_cur_b:
                app_cur_b.execute("SELECT id FROM pets WHERE id = %s", (pb,))
                assert app_cur_b.fetchone() is not None

                app_cur_b.execute("SELECT id FROM pets WHERE id = %s", (pa,))
                assert app_cur_b.fetchone() is None
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()
