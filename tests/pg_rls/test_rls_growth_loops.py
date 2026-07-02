"""
Suíte P2 — isolamento cross-tenant em Postgres real. Bloco 2: growth loops.

Tabelas das cunhas ①②③ (migrations 0077, 0080, 0081 + coupon_redemptions
da 0015/0043).

Ativação: PG_TEST_DATABASE_URL deve estar definida (ver conftest.py).

Cobertura:
  - T05  webhook_events só acessível com escopo global '*'
  - T06  webhook_events com tenant específico = 0 linhas
  - T07  walker_referrals isolados por tenant (USING)
  - T08  walker_referrals WITH CHECK rejeita tenant_id errado
  - T09  coupon_redemptions isoladas por tenant (USING)
  - T10  coupon_redemptions WITH CHECK rejeita tenant_id errado
"""
import psycopg2
import pytest

from tests.pg_rls.conftest import (
    app_session,
    make_uid,
    setup_tenants,
    setup_user,
)


class TestGrowthLoopsTables:
    """T05-T10: webhook_events, walker_referrals, coupon_redemptions."""

    def test_T05_webhook_events_only_global_scope(self, owner_tx):
        """webhook_events: escopo '*' lê a linha inserida pelo owner."""
        cur = owner_tx.cursor()
        ev_id = make_uid()
        wid = make_uid()
        cur.execute(
            """
            INSERT INTO webhook_events (id, event_id, provider, event_type, created_at)
            VALUES (%s, %s, 'asaas', 'PAYMENT_CONFIRMED', NOW())
            """,
            (wid, ev_id),
        )
        owner_tx.commit()

        try:
            with app_session("*") as app_cur:
                app_cur.execute(
                    "SELECT id FROM webhook_events WHERE id = %s", (wid,)
                )
                assert app_cur.fetchone() is not None, (
                    "webhook_event não visível no escopo global"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM webhook_events WHERE id = %s", (wid,))
            owner_tx.commit()

    def test_T06_webhook_events_blocked_for_tenant_scope(self, owner_tx):
        """webhook_events: escopo de tenant específico = 0 linhas."""
        cur = owner_tx.cursor()
        ev_id = make_uid()
        wid = make_uid()
        cur.execute(
            """
            INSERT INTO webhook_events (id, event_id, provider, event_type, created_at)
            VALUES (%s, %s, 'asaas', 'PAYMENT_CONFIRMED', NOW())
            """,
            (wid, ev_id),
        )
        ta, _ = setup_tenants(cur)
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM webhook_events WHERE id = %s", (wid,)
                )
                assert app_cur.fetchone() is None, (
                    "webhook_event visível para escopo de tenant — policy errada"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM webhook_events WHERE id = %s", (wid,))
            cur2.execute("DELETE FROM tenants WHERE id = %s", (ta,))
            owner_tx.commit()

    def test_T07_walker_referrals_isolated_by_tenant(self, owner_tx):
        """walker_referrals: tenant A não enxerga referrals do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)

        # A tabela pode não ter tenant_id se a migration 0081 (defensiva)
        # não tiver sido aplicada. Verificar.
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='walker_referrals' AND column_name='tenant_id'"
        )
        has_tenant_col = cur.fetchone() is not None

        if not has_tenant_col:
            pytest.skip("walker_referrals.tenant_id não existe — migration 0081 não aplicada")

        ra, rb = make_uid(), make_uid()
        for rid, uid, tid in [(ra, ua, ta), (rb, ub, tb)]:
            cur.execute(
                """
                INSERT INTO walker_referrals
                    (id, referrer_user_id, referred_name, referred_phone,
                     referred_phone_normalized, city, neighborhood,
                     referral_code, status, reward_status,
                     completed_walks_count, tenant_id, created_at, updated_at)
                VALUES (%s, %s, 'Indicado Teste', '71999990000',
                        '71999990000', 'Salvador', 'Centro',
                        %s, 'pending', 'not_eligible', 0, %s, NOW(), NOW())
                """,
                (rid, uid, f"CODE-{rid[:6]}", tid),
            )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM walker_referrals WHERE id = %s", (rb,)
                )
                assert app_cur.fetchone() is None, (
                    "Tenant A enxerga referral do tenant B"
                )
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM walker_referrals WHERE id = %s", (ra,)
                )
                assert app_cur.fetchone() is not None, (
                    "Tenant A não enxerga seu próprio referral"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM walker_referrals WHERE id IN (%s, %s)", (ra, rb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T08_walker_referrals_with_check(self, owner_tx):
        """walker_referrals: INSERT com tenant_id errado viola WITH CHECK."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='walker_referrals' AND column_name='tenant_id'"
        )
        if not cur.fetchone():
            owner_tx.rollback()
            pytest.skip("walker_referrals.tenant_id não existe — migration 0081 não aplicada")

        owner_tx.commit()

        rid = make_uid()
        try:
            with app_session(ta) as app_cur:
                with pytest.raises((psycopg2.errors.CheckViolation,
                                    psycopg2.errors.InsufficientPrivilege)):
                    app_cur.execute(
                        """
                        INSERT INTO walker_referrals
                            (id, referrer_user_id, referred_name, referred_phone,
                             referred_phone_normalized, city, neighborhood,
                             referral_code, status, reward_status,
                             completed_walks_count, tenant_id,
                             created_at, updated_at)
                        VALUES (%s, %s, 'Indicado Teste', '71999990001',
                                '71999990001', 'Salvador', 'Centro',
                                %s, 'pending', 'not_eligible', 0, %s,
                                NOW(), NOW())
                        """,
                        (rid, ua, f"CODE-{rid[:6]}", tb),  # sessão=ta mas tenant_id=tb
                    )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM walker_referrals WHERE id = %s", (rid,))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T09_coupon_redemptions_isolated(self, owner_tx):
        """coupon_redemptions: tenant A não enxerga resgates do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        ub = setup_user(cur, tb)

        # Criar cupons em cada tenant.
        ca, cb = make_uid(), make_uid()
        for cid, tid in [(ca, ta), (cb, tb)]:
            cur.execute(
                """
                INSERT INTO coupons (id, tenant_id, code, discount_type,
                                     discount_value, min_amount, uses_count,
                                     active, is_referral_gift,
                                     created_at, updated_at)
                VALUES (%s, %s, %s, 'percent', 10, 0.0, 0,
                        true, false, NOW(), NOW())
                """,
                (cid, tid, f"COUP-{cid[:6]}"),
            )

        ra, rb = make_uid(), make_uid()
        for rid, cid, uid, tid in [(ra, ca, ua, ta), (rb, cb, ub, tb)]:
            cur.execute(
                """
                INSERT INTO coupon_redemptions (id, coupon_id, tenant_id, user_id,
                                                amount_discounted,
                                                single_use_per_user, created_at)
                VALUES (%s, %s, %s, %s, 5.0, true, NOW())
                """,
                (rid, cid, tid, uid),
            )
        owner_tx.commit()

        try:
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM coupon_redemptions WHERE id = %s", (rb,)
                )
                assert app_cur.fetchone() is None, (
                    "Tenant A enxerga resgate do tenant B"
                )
            with app_session(ta) as app_cur:
                app_cur.execute(
                    "SELECT id FROM coupon_redemptions WHERE id = %s", (ra,)
                )
                assert app_cur.fetchone() is not None, (
                    "Tenant A não enxerga seu próprio resgate"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM coupon_redemptions WHERE id IN (%s, %s)", (ra, rb)
            )
            cur2.execute("DELETE FROM coupons WHERE id IN (%s, %s)", (ca, cb))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T10_coupon_redemptions_with_check(self, owner_tx):
        """coupon_redemptions: INSERT com tenant_id errado viola WITH CHECK."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)

        ca = make_uid()
        cur.execute(
            """
            INSERT INTO coupons (id, tenant_id, code, discount_type,
                                 discount_value, min_amount, uses_count,
                                 active, is_referral_gift,
                                 created_at, updated_at)
            VALUES (%s, %s, 'TESTCOUP', 'percent', 10, 0.0, 0,
                    true, false, NOW(), NOW())
            """,
            (ca, ta),
        )
        owner_tx.commit()

        rid = make_uid()
        try:
            with app_session(ta) as app_cur:
                with pytest.raises((psycopg2.errors.CheckViolation,
                                    psycopg2.errors.InsufficientPrivilege)):
                    app_cur.execute(
                        """
                        INSERT INTO coupon_redemptions (id, coupon_id, tenant_id,
                                                        user_id, amount_discounted,
                                                        single_use_per_user,
                                                        created_at)
                        VALUES (%s, %s, %s, %s, 5.0, true, NOW())
                        """,
                        (rid, ca, tb, ua),  # sessão=ta mas tenant_id=tb
                    )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM coupon_redemptions WHERE id = %s", (rid,))
            cur2.execute("DELETE FROM coupons WHERE id = %s", (ca,))
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()
