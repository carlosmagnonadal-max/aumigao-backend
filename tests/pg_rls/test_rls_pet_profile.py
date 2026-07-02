"""
Suíte P2 — isolamento cross-tenant em Postgres real. Bloco 3: Perfil Vivo do Pet.

Tabelas das migrations 0073-0076: pet_timeline_events, pet_profile_configs,
walk_observations, pet_reminders, pet_share_links.

Ativação: PG_TEST_DATABASE_URL deve estar definida (ver conftest.py).

Cobertura:
  - T11  pet_timeline_events isolados por tenant
  - T12  pet_timeline_events WITH CHECK rejeita tenant errado
  - T13  pet_profile_configs isolados por tenant
  - T14  walk_observations isoladas por tenant
  - T15  pet_reminders isolados por tenant
  - T16  pet_share_links isolados por tenant (tenant_id NULL = global)
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


class TestPetLiveProfileTables:
    """T11-T16: tabelas do Perfil Vivo do Pet (0073-0076)."""

    def test_T11_pet_timeline_events_isolated(self, owner_tx):
        """pet_timeline_events: tenant A não enxerga eventos do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)

        ea, eb = make_uid(), make_uid()
        for eid, pid, tid in [(ea, pa, ta), (eb, pb, tb)]:
            cur.execute(
                """
                INSERT INTO pet_timeline_events
                    (id, pet_id, tenant_id, event_type, title, occurred_at, created_at)
                VALUES (%s, %s, %s, 'health', 'Vacina', NOW(), NOW())
                """,
                (eid, pid, tid),
            )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_timeline_events WHERE id = %s", (eb,)
                )
                assert app_cur.fetchone() is None, (
                    "Tenant A enxerga evento do tenant B"
                )
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_timeline_events WHERE id = %s", (ea,)
                )
                assert app_cur.fetchone() is not None, (
                    "Tenant A não enxerga seu próprio evento"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM pet_timeline_events WHERE id IN (%s, %s)", (ea, eb)
            )
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T12_pet_timeline_events_with_check(self, owner_tx):
        """pet_timeline_events: INSERT com tenant_id errado viola WITH CHECK."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        pa = setup_pet(cur, ta, ua)
        owner_tx.commit()

        eid = make_uid()
        try:
            with app_session(ta) as app_cur:
                with pytest.raises(psycopg2.errors.CheckViolation):
                    app_cur.execute(
                        """
                        INSERT INTO pet_timeline_events
                            (id, pet_id, tenant_id, event_type, title,
                             occurred_at, created_at)
                        VALUES (%s, %s, %s, 'health', 'Intruso', NOW(), NOW())
                        """,
                        (eid, pa, tb),  # sessão=ta mas tenant_id=tb
                    )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM pet_timeline_events WHERE id = %s", (eid,))
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T13_pet_profile_configs_isolated(self, owner_tx):
        """pet_profile_configs: tenant A não enxerga config do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)

        ca, cb = make_uid(), make_uid()
        for cid, tid in [(ca, ta), (cb, tb)]:
            cur.execute(
                """
                INSERT INTO pet_profile_configs
                    (id, tenant_id, profile_enabled, observations_enabled,
                     reminders_enabled, share_enabled, created_at, updated_at)
                VALUES (%s, %s, false, false, false, false, NOW(), NOW())
                ON CONFLICT (tenant_id) DO NOTHING
                """,
                (cid, tid),
            )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_profile_configs WHERE id = %s", (cb,)
                )
                assert app_cur.fetchone() is None, (
                    "Tenant A enxerga config do tenant B"
                )
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_profile_configs WHERE id = %s", (ca,)
                )
                assert app_cur.fetchone() is not None, (
                    "Tenant A não enxerga sua própria config"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM pet_profile_configs WHERE id IN (%s, %s)", (ca, cb)
            )
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T14_walk_observations_isolated(self, owner_tx):
        """walk_observations: tenant A não enxerga observações do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)

        # Precisamos de walks para FK.
        wa, wb = make_uid(), make_uid()
        for wid, tid, uid, pid in [(wa, ta, ua, pa), (wb, tb, ub, pb)]:
            cur.execute(
                """
                INSERT INTO walks (id, tenant_id, tutor_id, pet_id, walker_id,
                                   status, scheduled_date, duration_minutes, price,
                                   created_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', '2026-01-01', 30, 0.0,
                        NOW())
                """,
                (wid, tid, uid, pid, uid),
            )

        oa, ob = make_uid(), make_uid()
        for oid, wid, pid, tid, uid in [(oa, wa, pa, ta, ua), (ob, wb, pb, tb, ub)]:
            cur.execute(
                """
                INSERT INTO walk_observations
                    (id, walk_id, pet_id, tenant_id, walker_user_id,
                     incident, created_at)
                VALUES (%s, %s, %s, %s, %s, false, NOW())
                """,
                (oid, wid, pid, tid, uid),
            )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM walk_observations WHERE id = %s", (ob,)
                )
                assert app_cur.fetchone() is None, (
                    "Tenant A enxerga observação do tenant B"
                )
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM walk_observations WHERE id = %s", (oa,)
                )
                assert app_cur.fetchone() is not None, (
                    "Tenant A não enxerga sua própria observação"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM walk_observations WHERE id IN (%s, %s)", (oa, ob)
            )
            cur2.execute("DELETE FROM walks WHERE id IN (%s, %s)", (wa, wb))
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T15_pet_reminders_isolated(self, owner_tx):
        """pet_reminders: tenant A não enxerga lembretes do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)

        ra, rb = make_uid(), make_uid()
        for rid, pid, tid in [(ra, pa, ta), (rb, pb, tb)]:
            cur.execute(
                """
                INSERT INTO pet_reminders (id, pet_id, tenant_id, kind, due_date,
                                           active, created_at)
                VALUES (%s, %s, %s, 'vaccine', CURRENT_DATE + 30, true, NOW())
                """,
                (rid, pid, tid),
            )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_reminders WHERE id = %s", (rb,)
                )
                assert app_cur.fetchone() is None, (
                    "Tenant A enxerga lembrete do tenant B"
                )
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_reminders WHERE id = %s", (ra,)
                )
                assert app_cur.fetchone() is not None, (
                    "Tenant A não enxerga seu próprio lembrete"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM pet_reminders WHERE id IN (%s, %s)", (ra, rb)
            )
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T16_pet_share_links_isolated(self, owner_tx):
        """pet_share_links: tenant A não enxerga links do tenant B.
        tenant_id=NULL é permitido (link público) e visível no escopo global."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)
        pa = setup_pet(cur, ta, ua)
        pb = setup_pet(cur, tb, ub)

        la, lb, lpub = make_uid(), make_uid(), make_uid()
        # la = link do tenant A, lb = link do tenant B, lpub = link público (NULL)
        cur.execute(
            """
            INSERT INTO pet_share_links (id, token, pet_id, tenant_id, created_by,
                                         consent_at, expires_at, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW() + interval '30d', NOW())
            """,
            (la, f"tok-{la[:8]}", pa, ta, ua),
        )
        cur.execute(
            """
            INSERT INTO pet_share_links (id, token, pet_id, tenant_id, created_by,
                                         consent_at, expires_at, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW(), NOW() + interval '30d', NOW())
            """,
            (lb, f"tok-{lb[:8]}", pb, tb, ub),
        )
        cur.execute(
            """
            INSERT INTO pet_share_links (id, token, pet_id, tenant_id, created_by,
                                         consent_at, expires_at, created_at)
            VALUES (%s, %s, %s, NULL, %s, NOW(), NOW() + interval '30d', NOW())
            """,
            (lpub, f"tok-{lpub[:8]}", pa, ua),
        )
        owner_tx.commit()

        try:
            # Tenant A não enxerga link do tenant B.
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_share_links WHERE id = %s", (lb,)
                )
                assert app_cur.fetchone() is None, (
                    "Tenant A enxerga pet_share_link do tenant B"
                )

            # Tenant A enxerga seu próprio link.
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_share_links WHERE id = %s", (la,)
                )
                assert app_cur.fetchone() is not None

            # Link público (tenant_id=NULL) visível para qualquer tenant (NULL allowance).
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_share_links WHERE id = %s", (lpub,)
                )
                assert app_cur.fetchone() is not None, (
                    "Link público (tenant_id=NULL) não visível para tenant A"
                )

            # Link público visível no escopo global.
            with app_session("*") as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_share_links WHERE id = %s", (lpub,)
                )
                assert app_cur.fetchone() is not None
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM pet_share_links WHERE id IN (%s, %s, %s)", (la, lb, lpub)
            )
            cur2.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()
