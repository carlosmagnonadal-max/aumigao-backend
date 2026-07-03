"""
Suíte P2 — isolamento cross-tenant em Postgres real. Bloco 4: "pets seguem o tutor".

Migration 0093: a ficha e a SAÚDE do pet seguem o TUTOR para QUALQUER tenant onde ele
tem vínculo ATIVO (tenant_tutor_access.status='active'); o histórico OPERACIONAL
(walk_observations e os eventos walk_observation/tenant_note na timeline) NÃO segue.

Ativação: PG_TEST_DATABASE_URL deve estar definida (ver conftest.py). Sem ela toda a
suíte pg_rls é ignorada — daí o cuidado redobrado nestes testes (só o CI os exercita).

Cobertura (numerada na sequência da suíte, T20/T21 já usados em test_rls_pet_profile):
  - T22  pet do tenant A VISÍVEL no tenant B quando há vínculo ATIVO do tutor
  - T23  pet INVISÍVEL no tenant B sem vínculo (e com vínculo revogado/pending)
  - T24  health_record SEGUE o pet (visível no tenant B com vínculo ativo)
  - T25  walk_observation NÃO segue (invisível no tenant B mesmo com vínculo)
  - T26  ramo DONO: tutor vê o pet via current_user_id mesmo SEM vínculo
  - T27  WITH CHECK: UPDATE do pet do tenant A sob sessão B com vínculo funciona;
         INSERT forjando tenant_id de terceiro continua bloqueado
  - T28  timeline: evento de saúde SEGUE; evento operacional (walk_observation) NÃO
"""
import psycopg2
import pytest

from tests.pg_rls.conftest import (
    app_session,
    app_session_as,
    make_uid,
    setup_health_record,
    setup_pet,
    setup_tenants,
    setup_timeline_event,
    setup_tutor_link,
    setup_user,
    setup_walk_observation,
)


def _cleanup(owner_tx, ta, tb, *, extra=()):
    """Limpeza padrão pós-teste (ordem FK-safe). `extra` = statements SQL adicionais."""
    cur = owner_tx.cursor()
    for stmt, params in extra:
        cur.execute(stmt, params)
    cur.execute("DELETE FROM walk_observations WHERE tenant_id IN (%s, %s)", (ta, tb))
    cur.execute("DELETE FROM walks WHERE tenant_id IN (%s, %s)", (ta, tb))
    cur.execute("DELETE FROM pet_timeline_events WHERE tenant_id IN (%s, %s)", (ta, tb))
    cur.execute("DELETE FROM pet_health_records WHERE tenant_id IN (%s, %s)", (ta, tb))
    cur.execute("DELETE FROM tenant_tutor_access WHERE tenant_id IN (%s, %s)", (ta, tb))
    cur.execute("DELETE FROM pets WHERE tenant_id IN (%s, %s)", (ta, tb))
    cur.execute("DELETE FROM users WHERE tenant_id IN (%s, %s)", (ta, tb))
    cur.execute("DELETE FROM tenants WHERE id IN (%s, %s)", (ta, tb))
    owner_tx.commit()


class TestPetsFollowTutor:
    """T22-T28: pets/saúde seguem o tutor; operacional fica por tenant (0093)."""

    def test_T22_pet_visible_in_linked_tenant(self, owner_tx):
        """Pet criado no tenant A é VISÍVEL sob escopo do tenant B com vínculo ATIVO."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        tutor = setup_user(cur, ta)
        pet = setup_pet(cur, ta, tutor)
        setup_tutor_link(cur, tb, tutor, status="active")
        owner_tx.commit()

        try:
            with app_session(tb) as app_cur:
                app_cur.execute("SELECT id FROM pets WHERE id = %s", (pet,))
                assert app_cur.fetchone() is not None, (
                    "Pet do tutor não seguiu para o tenant B vinculado"
                )
        finally:
            _cleanup(owner_tx, ta, tb)

    def test_T23_pet_invisible_without_active_link(self, owner_tx):
        """Sem vínculo → invisível; vínculo revogado/pending → também invisível."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        tutor = setup_user(cur, ta)
        pet = setup_pet(cur, ta, tutor)
        owner_tx.commit()

        try:
            # Sem vínculo algum.
            with app_session(tb) as app_cur:
                app_cur.execute("SELECT id FROM pets WHERE id = %s", (pet,))
                assert app_cur.fetchone() is None, "Pet visível no tenant B SEM vínculo"

            # Vínculo com status != active não libera.
            for bad_status in ("revoked", "pending", "declined"):
                cur2 = owner_tx.cursor()
                cur2.execute("DELETE FROM tenant_tutor_access WHERE tenant_id = %s", (tb,))
                owner_tx.commit()
                setup_tutor_link(cur2, tb, tutor, status=bad_status)
                owner_tx.commit()
                with app_session(tb) as app_cur:
                    app_cur.execute("SELECT id FROM pets WHERE id = %s", (pet,))
                    assert app_cur.fetchone() is None, (
                        f"Pet visível no tenant B com vínculo status={bad_status!r}"
                    )
        finally:
            _cleanup(owner_tx, ta, tb)

    def test_T24_health_record_follows_pet(self, owner_tx):
        """health_record do pet SEGUE: visível no tenant B com vínculo ativo do tutor."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        tutor = setup_user(cur, ta)
        pet = setup_pet(cur, ta, tutor)
        rec = setup_health_record(cur, ta, pet)
        setup_tutor_link(cur, tb, tutor, status="active")
        owner_tx.commit()

        try:
            with app_session(tb) as app_cur:
                app_cur.execute("SELECT id FROM pet_health_records WHERE id = %s", (rec,))
                assert app_cur.fetchone() is not None, (
                    "Registro de saúde não seguiu para o tenant B vinculado"
                )
            # Sem vínculo continua invisível (controle negativo).
            cur2 = owner_tx.cursor()
            cur2.execute("DELETE FROM tenant_tutor_access WHERE tenant_id = %s", (tb,))
            owner_tx.commit()
            with app_session(tb) as app_cur:
                app_cur.execute("SELECT id FROM pet_health_records WHERE id = %s", (rec,))
                assert app_cur.fetchone() is None, (
                    "Registro de saúde visível no tenant B SEM vínculo"
                )
        finally:
            _cleanup(owner_tx, ta, tb)

    def test_T25_walk_observation_does_not_follow(self, owner_tx):
        """walk_observation NÃO segue: invisível no tenant B mesmo com vínculo ativo."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        tutor = setup_user(cur, ta)
        walker = setup_user(cur, ta)
        pet = setup_pet(cur, ta, tutor)
        obs, _walk = setup_walk_observation(cur, ta, pet, walker)
        setup_tutor_link(cur, tb, tutor, status="active")
        owner_tx.commit()

        try:
            with app_session(tb) as app_cur:
                app_cur.execute("SELECT id FROM walk_observations WHERE id = %s", (obs,))
                assert app_cur.fetchone() is None, (
                    "walk_observation seguiu o tutor (deveria ficar no tenant de origem)"
                )
            # Sanidade: no tenant de origem A ela é visível.
            with app_session(ta) as app_cur:
                app_cur.execute("SELECT id FROM walk_observations WHERE id = %s", (obs,))
                assert app_cur.fetchone() is not None, (
                    "walk_observation não visível no próprio tenant de origem"
                )
        finally:
            _cleanup(owner_tx, ta, tb)

    def test_T26_owner_branch_sees_pet_without_link(self, owner_tx):
        """Ramo DONO: o tutor (current_user_id) vê o pet mesmo sob escopo de outro
        tenant SEM vínculo — a identidade do dono basta (guard NOT IN ('-',''))."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        tutor = setup_user(cur, ta)
        pet = setup_pet(cur, ta, tutor)
        owner_tx.commit()

        try:
            # Escopo tenant B, current_user_id = tutor, SEM vínculo → ramo dono libera.
            with app_session_as(tb, tutor) as app_cur:
                app_cur.execute("SELECT id FROM pets WHERE id = %s", (pet,))
                assert app_cur.fetchone() is not None, (
                    "Ramo DONO não liberou o pet para o próprio tutor sob escopo B"
                )
            # Guard: sem current_user_id (app_session puro) e sem vínculo → invisível.
            with app_session(tb) as app_cur:
                app_cur.execute("SELECT id FROM pets WHERE id = %s", (pet,))
                assert app_cur.fetchone() is None, (
                    "Pet visível sob escopo B sem dono nem vínculo (guard falhou)"
                )
        finally:
            _cleanup(owner_tx, ta, tb)

    def test_T27_with_check_update_ok_insert_forge_blocked(self, owner_tx):
        """WITH CHECK: UPDATE do pet do tenant A sob sessão B (com vínculo) funciona;
        INSERT de pet cujo TUTOR NÃO tem vínculo com o tenant da sessão é bloqueado.

        NOTA (comportamento documentado da 0093): o ramo de VÍNCULO do WITH CHECK
        compara o vínculo do tutor com o tenant da SESSÃO (não com o tenant_id da
        linha) — necessário para o UPDATE legítimo do pet seguido (linha mantém
        tenant_id=ta sob sessão tb). Consequência: com vínculo ativo, o RLS sozinho
        NÃO impede gravar tenant_id de terceiro; essa defesa fica na camada de
        aplicação (PetCreate/PetUpdate não expõem tenant_id — carimbo server-side).
        Mesmo trade-off do precedente 0092 (users)."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        tc = make_uid()  # terceiro tenant para o tenant_id forjado
        cur.execute(
            """
            INSERT INTO tenants (id, slug, name, plan, status, created_at, updated_at)
            VALUES (%s, %s, %s, 'pro', 'active', NOW(), NOW())
            """,
            (tc, f"slug-{tc[:8]}", f"Tenant {tc[:8]}"),
        )
        tutor = setup_user(cur, ta)
        pet = setup_pet(cur, ta, tutor)
        setup_tutor_link(cur, tb, tutor, status="active")
        # tutor2 NÃO tem vínculo com tb — todos os ramos do WITH CHECK falham pra ele.
        tutor2 = setup_user(cur, tc)
        owner_tx.commit()

        try:
            # UPDATE sob escopo B (pet segue via vínculo) — USING e WITH CHECK passam
            # porque o pet mantém tenant_id=ta e o ramo vínculo casa nos dois lados.
            with app_session(tb) as app_cur:
                app_cur.execute(
                    "UPDATE pets SET behavior_notes = 'via B' WHERE id = %s", (pet,)
                )
                assert app_cur.rowcount == 1, (
                    "UPDATE do pet seguido sob escopo B não afetou a linha"
                )
            # INSERT sob sessão B de pet de tutor SEM vínculo com B, forjando
            # tenant_id=tc: nenhum ramo casa (nem escopo, nem NULL, nem dono — GUC de
            # user ausente —, nem vínculo do tutor2 com tb) → WITH CHECK rejeita.
            intruder = make_uid()
            with app_session(tb) as app_cur:
                with pytest.raises((psycopg2.errors.CheckViolation,
                                    psycopg2.errors.InsufficientPrivilege)):
                    app_cur.execute(
                        """
                        INSERT INTO pets (id, tenant_id, tutor_id, name, species,
                                          sex, breed, size, behavior_notes,
                                          is_social, afraid_of_noise, pulls_leash,
                                          can_walk_with_other_pets, is_neutered,
                                          allergies, medications, restrictions,
                                          health_notes, weight, created_at)
                        VALUES (%s, %s, %s, 'Intruso', 'dog', 'M', 'SRD', 'M', '',
                                true, false, false, false, false,
                                '', '', '', '', 5.0, NOW())
                        """,
                        (intruder, tc, tutor2),  # sessão=tb, tutor sem vínculo com tb
                    )
        finally:
            _cleanup(owner_tx, ta, tb, extra=(
                ("DELETE FROM pets WHERE tenant_id = %s", (tc,)),
                ("DELETE FROM users WHERE tenant_id = %s", (tc,)),
                ("DELETE FROM tenants WHERE id = %s", (tc,)),
            ))

    def test_T28_timeline_health_follows_operational_does_not(self, owner_tx):
        """Timeline: evento de SAÚDE (health_note) segue o tutor; evento OPERACIONAL
        (walk_observation) NÃO segue — mesmo com vínculo ativo no tenant B."""
        cur = owner_tx.cursor()
        ta, tb = setup_tenants(cur)
        tutor = setup_user(cur, ta)
        pet = setup_pet(cur, ta, tutor)
        ev_health = setup_timeline_event(cur, ta, pet, event_type="health_note")
        ev_op = setup_timeline_event(cur, ta, pet, event_type="walk_observation")
        ev_tenant_note = setup_timeline_event(cur, ta, pet, event_type="tenant_note")
        setup_tutor_link(cur, tb, tutor, status="active")
        owner_tx.commit()

        try:
            with app_session(tb) as app_cur:
                app_cur.execute(
                    "SELECT id FROM pet_timeline_events WHERE id = %s", (ev_health,)
                )
                assert app_cur.fetchone() is not None, (
                    "Evento de saúde não seguiu o tutor para o tenant B"
                )
                app_cur.execute(
                    "SELECT id FROM pet_timeline_events WHERE id = %s", (ev_op,)
                )
                assert app_cur.fetchone() is None, (
                    "Evento operacional (walk_observation) seguiu o tutor (não deveria)"
                )
                app_cur.execute(
                    "SELECT id FROM pet_timeline_events WHERE id = %s", (ev_tenant_note,)
                )
                assert app_cur.fetchone() is None, (
                    "Evento operacional (tenant_note) seguiu o tutor (não deveria)"
                )
            # No tenant de origem A todos são visíveis (controle).
            with app_session(ta) as app_cur:
                for eid in (ev_health, ev_op, ev_tenant_note):
                    app_cur.execute(
                        "SELECT id FROM pet_timeline_events WHERE id = %s", (eid,)
                    )
                    assert app_cur.fetchone() is not None, (
                        f"Evento {eid} invisível no próprio tenant de origem"
                    )
        finally:
            _cleanup(owner_tx, ta, tb)
