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
    app_session_as,
    make_uid,
    setup_pet,
    setup_tenants,
    setup_tutor_link,
    setup_user,
    setup_walker_link,
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


class TestUsersSelfIdentity:
    """T20-T22: identidade GLOBAL da tabela `users` (migration 0091).

    Modelo B: o usuário nasce no tenant A mas troca de tenant no app (escopo RLS B).
    A policy self-identity deve permitir resolver a PRÓPRIA linha sob QUALQUER escopo
    de tenant, SEM afrouxar o isolamento das demais linhas de `users`.
    """

    def test_T20_self_row_visible_under_foreign_tenant_scope(self, owner_tx):
        """(Req 1a) Usuário do tenant A, sob escopo RLS do tenant B, RESOLVE a própria
        linha via app.current_user_id — reproduz o get_current_user do fix."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)  # usuário nasce no tenant A
        owner_tx.commit()

        try:
            # Escopo RLS = tenant B (troca de tenant no app), current_user_id = ua.
            with app_session_as(tb, ua) as app_cur:
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ua,))
                assert app_cur.fetchone() is not None, (
                    "Usuário do tenant A não foi resolvido sob escopo do tenant B — "
                    "o fix da identidade global falhou (regressão do bug de 401)"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T21_other_users_still_isolated_under_foreign_scope(self, owner_tx):
        """(Req 1b) Sob escopo do tenant B com current_user_id = ua:
        - OUTROS usuários do tenant A (origem do usuário) ficam INVISÍVEIS — a
          ampliação da policy vale apenas para a própria linha;
        - usuários do tenant B continuam visíveis: é o comportamento PRÉ-EXISTENTE
          do escopo por tenant (`tenant_id = current_tenant`) e é necessário para a
          operação (ex.: tutor lista passeadores do tenant ativo). A policy 0091 não
          amplia nem reduz isso.
        """
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)   # o "eu" (tenant A)
        ua2 = setup_user(cur, ta)  # OUTRO usuário do tenant A
        ub = setup_user(cur, tb)   # usuário do tenant B (escopo atual)
        owner_tx.commit()

        try:
            with app_session_as(tb, ua) as app_cur:
                # Enxerga a própria linha.
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ua,))
                assert app_cur.fetchone() is not None

                # NÃO enxerga outro usuário do tenant A (mesmo sendo "meu" tenant de origem).
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ua2,))
                assert app_cur.fetchone() is None, (
                    "Usuário enxergou OUTRA linha do tenant A — vazamento cross-tenant"
                )

                # Usuário do tenant B VISÍVEL sob escopo B — semântica padrão do
                # tenant scoping, inalterada pela 0091 (não é vazamento).
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ub,))
                assert app_cur.fetchone() is not None, (
                    "Usuário do tenant B sumiu sob escopo B — a policy 0091 restringiu "
                    "além do desenhado (regressão do tenant scoping padrão)"
                )

                # SELECT geral: própria linha + usuários do tenant do escopo; nada do A.
                app_cur.execute(
                    "SELECT id FROM users WHERE id IN (%s, %s, %s)", (ua, ua2, ub)
                )
                found = {row[0] for row in app_cur.fetchall()}
                assert found == {ua, ub}, (
                    f"Conjunto visível divergente do esperado (própria linha + tenant do escopo): {found}"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T22_no_user_guc_still_fail_closed_on_users(self, owner_tx):
        """Sessão sem current_user_id (default '-') sob escopo do tenant B NÃO casa a
        linha de nenhum usuário do tenant A pelo ramo self-identity (NOT IN ('-',''))."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)
        owner_tx.commit()

        try:
            # app_session seta apenas o tenant; current_user_id fica ausente/'-'.
            with app_session(tb) as app_cur:
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ua,))
                assert app_cur.fetchone() is None, (
                    "Sem current_user_id, a linha do tenant A ficou visível sob "
                    "escopo B — o guard NOT IN ('-','') falhou"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()


class TestUsersMembershipVisibility:
    """T23-T25: MEMBERSHIP por vínculo ativo na tabela `users` (migration 0092).

    Modelo B: o usuário nasce no tenant A mas é MEMBRO do tenant B via vínculo
    (tenant_tutor_access / tenant_walker_access). Sob o escopo RLS do tenant B, a
    policy deve enxergar a linha do user vinculado (senão a contagem/listagem de
    tutores/walkers vinculados retorna 0), SEM afrouxar o isolamento das demais linhas.

    NB: os testes exercitam a policy SEM current_user_id (app_session, não
    app_session_as) — o ramo self-identity da 0091 fica DESLIGADO, isolando o efeito
    da 0092 (membership). É exatamente o cenário de produção: o admin do tenant lista
    usuários vinculados que NÃO são ele mesmo.
    """

    def test_T23_active_tutor_link_makes_user_visible(self, owner_tx):
        """(0092 — tutor) User nascido no tenant A, com vínculo ATIVO ao tenant B,
        fica VISÍVEL sob o escopo RLS do tenant B (reproduz a contagem/listagem do
        fix 6f6f17b sob RLS)."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua = setup_user(cur, ta)                  # tutor nasce no tenant A
        setup_tutor_link(cur, tb, ua, "active")   # vínculo ativo ao tenant B
        owner_tx.commit()

        try:
            with app_session(tb) as app_cur:  # escopo do tenant B, sem current_user_id
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ua,))
                assert app_cur.fetchone() is not None, (
                    "Tutor vinculado (tenant_tutor_access ativo) INVISÍVEL sob o escopo "
                    "do tenant do vínculo — a policy 0092 (membership) falhou; a "
                    "contagem/listagem de tutores vinculados voltaria a dar 0"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM tenant_tutor_access WHERE tenant_id IN (%s, %s)", (ta, tb)
            )
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T24_inactive_link_does_not_make_user_visible(self, owner_tx):
        """(0092 — isolamento) Vínculo NÃO-ativo (pending/revoked) NÃO torna o user
        visível: só membership ATIVO conta."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua_pending = setup_user(cur, ta)
        ua_revoked = setup_user(cur, ta)
        setup_tutor_link(cur, tb, ua_pending, "pending")
        setup_walker_link(cur, tb, ua_revoked, "revoked")
        owner_tx.commit()

        try:
            with app_session(tb) as app_cur:
                app_cur.execute(
                    "SELECT id FROM users WHERE id IN (%s, %s)",
                    (ua_pending, ua_revoked),
                )
                found = {row[0] for row in app_cur.fetchall()}
                assert found == set(), (
                    f"Vínculo inativo tornou user(s) visível sob o tenant do vínculo: "
                    f"{found} — a policy 0092 exige status='active'"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM tenant_tutor_access WHERE tenant_id IN (%s, %s)", (ta, tb)
            )
            cur2.execute(
                "DELETE FROM tenant_walker_access WHERE tenant_id IN (%s, %s)", (ta, tb)
            )
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()

    def test_T25_user_without_link_stays_isolated(self, owner_tx):
        """(0092 — isolamento) User de outro tenant SEM vínculo com o tenant do escopo
        continua INVISÍVEL. Também cobre o ramo walker: um vínculo walker ativo ao
        tenant B torna o walker visível, mas um user do tenant A sem vínculo nenhum não."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        ua_unlinked = setup_user(cur, ta)   # tenant A, SEM vínculo com B
        ua_walker = setup_user(cur, ta)     # tenant A, vínculo walker ATIVO com B
        setup_walker_link(cur, tb, ua_walker, "active")
        owner_tx.commit()

        try:
            with app_session(tb) as app_cur:
                # Sem vínculo → invisível (isolamento cross-tenant preservado).
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ua_unlinked,))
                assert app_cur.fetchone() is None, (
                    "User do tenant A SEM vínculo com o tenant B ficou visível — "
                    "vazamento cross-tenant introduzido pela 0092"
                )
                # Com vínculo walker ativo → visível (ramo walker da 0092).
                app_cur.execute("SELECT id FROM users WHERE id = %s", (ua_walker,))
                assert app_cur.fetchone() is not None, (
                    "Walker vinculado (tenant_walker_access ativo) INVISÍVEL sob o "
                    "escopo do tenant do vínculo — ramo walker da 0092 falhou"
                )
        finally:
            cur2 = owner_tx.cursor()
            cur2.execute(
                "DELETE FROM tenant_walker_access WHERE tenant_id IN (%s, %s)", (ta, tb)
            )
            cur2.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
            cur2.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
            owner_tx.commit()
