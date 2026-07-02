"""
Suíte P2 — isolamento cross-tenant em Postgres real.

Exercita as políticas RLS ativas (migration 0043 + 0073-0083) contra um
Postgres de verdade, onde o role aumigao_app tem NOBYPASSRLS.

Ativação: PG_TEST_DATABASE_URL deve estar definida (ver conftest.py).

Cobertura:
  Bloco 1: Tabelas core (pets, walks, payments)
    - T01  Leitura tenant A não enxerga linhas do tenant B
    - T02  INSERT com tenant_id errado viola WITH CHECK (excpetion)
    - T03  Escopo global '*' enxerga linhas dos dois tenants
    - T04  GUC ausente / vazio = fail-closed (0 linhas)

  Bloco 2: Tabelas growth loops (cunha ①②③)
    - T05  webhook_events só acessível com escopo global '*'
    - T06  webhook_events com tenant específico = 0 linhas
    - T07  walker_referrals isolados por tenant (USING)
    - T08  walker_referrals WITH CHECK rejeita tenant_id errado
    - T09  coupon_redemptions isoladas por tenant (USING)
    - T10  coupon_redemptions WITH CHECK rejeita tenant_id errado

  Bloco 3: Tabelas do Perfil Vivo do Pet (0073-0076)
    - T11  pet_timeline_events isolados por tenant
    - T12  pet_timeline_events WITH CHECK rejeita tenant errado
    - T13  pet_profile_configs isolados por tenant
    - T14  walk_observations isoladas por tenant
    - T15  pet_reminders isolados por tenant
    - T16  pet_share_links isolados por tenant (tenant_id NULL = global)

  Bloco 4: Comportamentos especiais
    - T17  NULL tenant_id em pet_timeline_events visível no escopo global
    - T18  NULL tenant_id em pet_timeline_events visível no escopo do tenant dono
    - T19  Troca de tenant mid-transação (SET LOCAL reseta entre txns)
"""
import uuid

import psycopg2
import pytest

from tests.pg_rls.conftest import app_session

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid() -> str:
    return str(uuid.uuid4())


def _setup_tenants(cur) -> tuple[str, str]:
    """Insere dois tenants distintos e retorna (tenant_a_id, tenant_b_id)."""
    ta, tb = _uid(), _uid()
    for tid in (ta, tb):
        cur.execute(
            """
            INSERT INTO tenants (id, slug, name, plan, active, created_at, updated_at)
            VALUES (%s, %s, %s, 'pro', true, NOW(), NOW())
            """,
            (tid, f"slug-{tid[:8]}", f"Tenant {tid[:8]}"),
        )
    return ta, tb


def _setup_user(cur, tenant_id: str) -> str:
    """Insere um usuário pertencente ao tenant e retorna o user_id."""
    uid = _uid()
    cur.execute(
        """
        INSERT INTO users (id, tenant_id, email, hashed_password, name,
                           cpf_encrypted, role, active, created_at, updated_at)
        VALUES (%s, %s, %s, 'hash', 'Test User', 'enc', 'tutor', true, NOW(), NOW())
        """,
        (uid, tenant_id, f"user-{uid[:8]}@test.com"),
    )
    return uid


def _setup_pet(cur, tenant_id: str, user_id: str) -> str:
    """Insere um pet pertencente ao tenant e retorna o pet_id."""
    pid = _uid()
    cur.execute(
        """
        INSERT INTO pets (id, tenant_id, tutor_user_id, name, species,
                          breed, weight_kg, active, created_at, updated_at)
        VALUES (%s, %s, %s, 'Rex', 'dog', 'SRD', 5.0, true, NOW(), NOW())
        """,
        (pid, tenant_id, user_id),
    )
    return pid


# ===========================================================================
# BLOCO 1 — Tabelas core
# ===========================================================================

class TestCoreIsolation:
    """T01-T04: pets, walks, payments."""

    def test_T01_read_isolation(self, owner_tx):
        """Tenant A não enxerga pets do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)
        pa = _setup_pet(cur, ta, ua)
        pb = _setup_pet(cur, tb, ub)
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        owner_tx.commit()

        pid = _uid()
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)
        pa = _setup_pet(cur, ta, ua)
        pb = _setup_pet(cur, tb, ub)
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
        ta, _ = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        pa = _setup_pet(cur, ta, ua)
        owner_tx.commit()

        try:
            # Conectar como app mas sem setar o GUC (permanece vazio).
            import psycopg2 as _pg2
            from tests.pg_rls.conftest import _app_kwargs
            conn = _pg2.connect(**_app_kwargs())
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


# ===========================================================================
# BLOCO 2 — Growth loops (webhook_events, walker_referrals, coupon_redemptions)
# ===========================================================================

class TestGrowthLoopsTables:
    """T05-T10: tabelas das cunhas ①②③."""

    def test_T05_webhook_events_only_global_scope(self, owner_tx):
        """webhook_events: escopo '*' lê a linha inserida pelo owner."""
        cur = owner_tx.cursor()
        ev_id = _uid()
        wid = _uid()
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
        ev_id = _uid()
        wid = _uid()
        cur.execute(
            """
            INSERT INTO webhook_events (id, event_id, provider, event_type, created_at)
            VALUES (%s, %s, 'asaas', 'PAYMENT_CONFIRMED', NOW())
            """,
            (wid, ev_id),
        )
        ta, _ = _setup_tenants(cur)
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)

        # Inserir walker_referrals — a tabela pode não ter walker_referrals
        # se ainda não existir (migration 0081 é defensiva). Verificar.
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='walker_referrals' AND column_name='tenant_id'"
        )
        has_tenant_col = cur.fetchone() is not None

        if not has_tenant_col:
            pytest.skip("walker_referrals.tenant_id não existe — migration 0081 não aplicada")

        ra, rb = _uid(), _uid()
        for rid, uid, tid in [(ra, ua, ta), (rb, ub, tb)]:
            cur.execute(
                """
                INSERT INTO walker_referrals
                    (id, referrer_walker_user_id, referred_walker_user_id,
                     status, tenant_id, created_at)
                VALUES (%s, %s, %s, 'pending', %s, NOW())
                """,
                (rid, uid, uid, tid),
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)

        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='walker_referrals' AND column_name='tenant_id'"
        )
        if not cur.fetchone():
            owner_tx.rollback()
            pytest.skip("walker_referrals.tenant_id não existe — migration 0081 não aplicada")

        owner_tx.commit()

        rid = _uid()
        try:
            with app_session(ta) as app_cur:
                with pytest.raises((psycopg2.errors.CheckViolation,
                                    psycopg2.errors.InsufficientPrivilege)):
                    app_cur.execute(
                        """
                        INSERT INTO walker_referrals
                            (id, referrer_walker_user_id, referred_walker_user_id,
                             status, tenant_id, created_at)
                        VALUES (%s, %s, %s, 'pending', %s, NOW())
                        """,
                        (rid, ua, ua, tb),  # sessão=ta mas tenant_id=tb
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)

        # Criar cupons em cada tenant.
        ca, cb = _uid(), _uid()
        for cid, tid in [(ca, ta), (cb, tb)]:
            cur.execute(
                """
                INSERT INTO coupons (id, tenant_id, code, discount_type,
                                     discount_value, active, created_at, updated_at)
                VALUES (%s, %s, %s, 'percent', 10, true, NOW(), NOW())
                """,
                (cid, tid, f"COUP-{cid[:6]}"),
            )

        ra, rb = _uid(), _uid()
        for rid, cid, uid, tid in [(ra, ca, ua, ta), (rb, cb, ub, tb)]:
            cur.execute(
                """
                INSERT INTO coupon_redemptions (id, coupon_id, tenant_id, user_id,
                                                amount_discounted, created_at)
                VALUES (%s, %s, %s, %s, 5.0, NOW())
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)

        ca = _uid()
        cur.execute(
            """
            INSERT INTO coupons (id, tenant_id, code, discount_type,
                                 discount_value, active, created_at, updated_at)
            VALUES (%s, %s, 'TESTCOUP', 'percent', 10, true, NOW(), NOW())
            """,
            (ca, ta),
        )
        owner_tx.commit()

        rid = _uid()
        try:
            with app_session(ta) as app_cur:
                with pytest.raises(psycopg2.errors.CheckViolation):
                    app_cur.execute(
                        """
                        INSERT INTO coupon_redemptions (id, coupon_id, tenant_id,
                                                        user_id, amount_discounted,
                                                        created_at)
                        VALUES (%s, %s, %s, %s, 5.0, NOW())
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


# ===========================================================================
# BLOCO 3 — Perfil Vivo do Pet (migrations 0073-0076)
# ===========================================================================

class TestPetLiveProfileTables:
    """T11-T16: pet_timeline_events, pet_profile_configs, walk_observations,
    pet_reminders, pet_share_links."""

    def test_T11_pet_timeline_events_isolated(self, owner_tx):
        """pet_timeline_events: tenant A não enxerga eventos do tenant B."""
        cur = owner_tx.cursor()
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)
        pa = _setup_pet(cur, ta, ua)
        pb = _setup_pet(cur, tb, ub)

        ea, eb = _uid(), _uid()
        for eid, pid, tid, uid in [(ea, pa, ta, ua), (eb, pb, tb, ub)]:
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        pa = _setup_pet(cur, ta, ua)
        owner_tx.commit()

        eid = _uid()
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
        ta, tb = _setup_tenants(cur)

        ca, cb = _uid(), _uid()
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)
        pa = _setup_pet(cur, ta, ua)
        pb = _setup_pet(cur, tb, ub)

        # Precisamos de walks para FK.
        wa, wb = _uid(), _uid()
        for wid, tid, uid, pid in [(wa, ta, ua, pa), (wb, tb, ub, pb)]:
            cur.execute(
                """
                INSERT INTO walks (id, tenant_id, tutor_user_id, pet_id, walker_user_id,
                                   status, scheduled_start, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', NOW(), NOW(), NOW())
                """,
                (wid, tid, uid, pid, uid),
            )

        oa, ob = _uid(), _uid()
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)
        pa = _setup_pet(cur, ta, ua)
        pb = _setup_pet(cur, tb, ub)

        ra, rb = _uid(), _uid()
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)
        pa = _setup_pet(cur, ta, ua)
        pb = _setup_pet(cur, tb, ub)

        la, lb, lpub = _uid(), _uid(), _uid()
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


# ===========================================================================
# BLOCO 4 — Comportamentos especiais
# ===========================================================================

class TestSpecialBehaviors:
    """T17-T19: NULL tenant_id, troca de tenant mid-transação."""

    def test_T17_null_tenant_visible_global_scope(self, owner_tx):
        """Evento com tenant_id=NULL é visível no escopo global '*'."""
        cur = owner_tx.cursor()
        ta, _ = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        pa = _setup_pet(cur, ta, ua)

        eid = _uid()
        cur.execute(
            """
            INSERT INTO pet_timeline_events
                (id, pet_id, tenant_id, event_type, title, occurred_at, created_at)
            VALUES (%s, %s, NULL, 'system', 'Evento Global', NOW(), NOW())
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
        ta, _ = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        pa = _setup_pet(cur, ta, ua)

        eid = _uid()
        cur.execute(
            """
            INSERT INTO pet_timeline_events
                (id, pet_id, tenant_id, event_type, title, occurred_at, created_at)
            VALUES (%s, %s, NULL, 'system', 'Evento Global', NOW(), NOW())
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
        ta, tb = _setup_tenants(cur)
        ua = _setup_user(cur, ta)
        ub = _setup_user(cur, tb)
        pa = _setup_pet(cur, ta, ua)
        pb = _setup_pet(cur, tb, ub)
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
