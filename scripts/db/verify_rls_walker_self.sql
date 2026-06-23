-- =============================================================================
-- verify_rls_walker_self.sql
-- Verificação de RLS — policy tenant_isolation (walks) + cláusula walker-self
-- =============================================================================
--
-- PROPÓSITO
--   Provar, em PostgreSQL real (Neon), o comportamento de isolamento da tabela
--   walks após a migration 0049, em TRÊS camadas:
--
--   T1 — Isolamento de tenant (garantia central do RLS): uma sessão escopada a
--        um tenant (current_user_id='-', i.e. não-walker) NÃO enxerga linhas de
--        outro tenant.
--
--   T2 — Cláusula walker-self da 0049 (defesa em profundidade): sob escopo de UM
--        tenant específico, o passeador enxerga TAMBÉM seus próprios walks em
--        OUTRO tenant (cross-tenant), sem ver os de terceiros.
--
--   T3 — Camada de aplicação (é assim que /walker/* roda em produção):
--        get_walker_self_db usa escopo GLOBAL '*' (o RLS fica PERMISSIVO de
--        propósito) e a QUERY da rota filtra walker_id == user. T3 prova que a
--        combinação '*' + filtro de app isola corretamente, e documenta que sob
--        '*' o RLS sozinho NÃO isola por passeador (por design).
--
-- POR QUE ISSO IMPORTA
--   Os testes de pytest rodam em SQLite, que NÃO implementa RLS. Esta é a única
--   verificação que exercita as policies de verdade, sob o role da aplicação.
--
-- COMO RODAR
--   Cole inteiro no Neon SQL Editor (conectado como neondb_owner).
--   NÃO-DESTRUTIVO: tudo vive dentro de um BEGIN...ROLLBACK — nada é persistido.
--
-- COMO LER O RESULTADO
--   - "PASS <id>" / "NOTE <id>" em NOTICE  → ok.
--   - "FAIL <id>" em EXCEPTION → asserção falhou; a transação aborta e nada
--     persiste. Se isso acontecer, é sinal de regressão de RLS — me avise.
--   - "=== TODAS AS ASSERCOES PASSARAM ===" no fim → tudo certo.
--
-- POLICY VERIFICADA (estado após 0049)
--   USING:
--     current_setting('app.current_tenant', true) = '*'
--     OR tenant_id::text = current_setting('app.current_tenant', true)
--     OR (
--       current_setting('app.current_user_id', true) NOT IN ('-', '')
--       AND ( walker_id::text = current_setting('app.current_user_id', true)
--             OR assigned_walker_id::text = current_setting('app.current_user_id', true) )
--     )
--   WITH CHECK (inalterado): tenant match OR '*'.
--
-- ROLE
--   aumigao_app — non-owner, sem BYPASSRLS. O RLS só restringe este role.
--   neondb_owner (dono) faz BYPASS: todos os INSERTs de setup rodam como dono
--   (antes de SET ROLE). As asserções rodam sob SET ROLE aumigao_app.
--
-- DADOS SINTÉTICOS (prefixo rlsverify_)
--   walkA = (tenant A, walker W)   walkB = (tenant B, walker W)   walkO = (tenant B, walker O)
--   assigned_walker_id = walker_id em todos (passeador aceito = quem executa).
--
-- SUPOSIÇÕES
--   1. aumigao_app tem SELECT em walks. (É o role da app.)
--   2. Nenhum trigger BEFORE INSERT em walks/users/pets/tenants rejeita os dados.
--   3. Colunas com default Python-side (SQLAlchemy) NÃO têm DEFAULT no DDL — por
--      isso TODA coluna NOT NULL recebe valor explícito.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- SEÇÃO 1: SETUP — dados sintéticos inseridos como dono (bypass RLS)
-- Todos os INSERTs ocorrem ANTES do primeiro SET ROLE aumigao_app.
-- ---------------------------------------------------------------------------

-- 1.1 Tenants (created_at/updated_at NOT NULL sem server_default — explicitar)
INSERT INTO tenants (id, name, slug, status, plan, network_access_addon, created_at, updated_at) VALUES
    ('rlsverify_tenantA', 'RLS Verify Tenant A', 'rlsverify-a', 'active', 'starter', false, now(), now()),
    ('rlsverify_tenantB', 'RLS Verify Tenant B', 'rlsverify-b', 'active', 'starter', false, now(), now());

-- 1.2 Usuários: 1 tutor + 2 walkers
INSERT INTO users (
    id, email, password_hash, tenant_id, full_name, role, is_active,
    token_version, must_change_password, created_at
) VALUES
    ('rlsverify_tutor',   'rlsverify_tutor@rlstest.invalid',   '$2b$12$rlsverifyhashplaceholdertutor000', 'rlsverify_tenantA', 'Tutor RLS Verify',   'tutor',  true, 0, false, now()),
    ('rlsverify_walkerW', 'rlsverify_walkerW@rlstest.invalid', '$2b$12$rlsverifyhashplaceholderwalkerW0', NULL,                'Walker W RLS Verify','walker', true, 0, false, now()),
    ('rlsverify_walkerO', 'rlsverify_walkerO@rlstest.invalid', '$2b$12$rlsverifyhashplaceholderwalkerO0', NULL,                'Walker O RLS Verify','walker', true, 0, false, now());

-- 1.3 Pet do tutor (todas as colunas NOT NULL sem server_default DDL preenchidas)
INSERT INTO pets (
    id, tutor_id, tenant_id, name, species, sex, breed, size,
    behavior_notes, allergies, medications, restrictions, health_notes,
    is_social, afraid_of_noise, pulls_leash, can_walk_with_other_pets, is_neutered,
    created_at
) VALUES (
    'rlsverify_pet', 'rlsverify_tutor', 'rlsverify_tenantA', 'Pet RLS Verify',
    'Cachorro', '', '', '', '', '', '', '', '',
    true, false, false, false, false, now()
);

-- 1.4 TenantWalkerAccess: W em A e B (ativo); O em B (ativo)
INSERT INTO tenant_walker_access (
    id, tenant_id, walker_user_id, access_type, status, requirements_met, initiated_by, created_at, updated_at
) VALUES
    ('rlsverify_twa_WA', 'rlsverify_tenantA', 'rlsverify_walkerW', 'shared_network', 'active', true, 'tenant', now(), now()),
    ('rlsverify_twa_WB', 'rlsverify_tenantB', 'rlsverify_walkerW', 'shared_network', 'active', true, 'tenant', now(), now()),
    ('rlsverify_twa_OB', 'rlsverify_tenantB', 'rlsverify_walkerO', 'shared_network', 'active', true, 'tenant', now(), now());

-- 1.5 Walks: walkA(A,W) walkB(B,W) walkO(B,O)
INSERT INTO walks (
    id, tutor_id, tenant_id, walker_id, assigned_walker_id, pet_id,
    scheduled_date, duration_minutes, price, status, pickup_method, modality,
    destination, address_snapshot, notes, created_at, operational_status,
    walker_selection_mode, current_attempt, max_attempts
) VALUES
    ('rlsverify_walkA', 'rlsverify_tutor', 'rlsverify_tenantA', 'rlsverify_walkerW', 'rlsverify_walkerW', 'rlsverify_pet',
     '2099-01-01', 30, 50.00, 'Agendado', 'Buscar em casa', 'standard', '', '{}', '', now(), 'ride_scheduled', 'auto', 0, 3),
    ('rlsverify_walkB', 'rlsverify_tutor', 'rlsverify_tenantB', 'rlsverify_walkerW', 'rlsverify_walkerW', 'rlsverify_pet',
     '2099-01-02', 30, 50.00, 'Agendado', 'Buscar em casa', 'standard', '', '{}', '', now(), 'ride_scheduled', 'auto', 0, 3),
    ('rlsverify_walkO', 'rlsverify_tutor', 'rlsverify_tenantB', 'rlsverify_walkerO', 'rlsverify_walkerO', 'rlsverify_pet',
     '2099-01-03', 30, 50.00, 'Agendado', 'Buscar em casa', 'standard', '', '{}', '', now(), 'ride_scheduled', 'auto', 0, 3);

-- ---------------------------------------------------------------------------
-- SEÇÃO 2: ASSERÇÕES — sob role aumigao_app (RLS ativo). Sem mais INSERTs.
-- set_config(...,true) = SET LOCAL (escopo da transação; compatível com pooler).
-- ---------------------------------------------------------------------------

SET ROLE aumigao_app;

DO $$
DECLARE
    v_count   INTEGER;
    v_walker  TEXT := 'rlsverify_walkerW';
    v_other   TEXT := 'rlsverify_walkerO';
    v_walkA   TEXT := 'rlsverify_walkA';
    v_walkB   TEXT := 'rlsverify_walkB';
    v_walkO   TEXT := 'rlsverify_walkO';
    v_tenantA TEXT := 'rlsverify_tenantA';
    v_tenantB TEXT := 'rlsverify_tenantB';
BEGIN
    -- =====================================================================
    -- T1 — Isolamento de tenant (sessão NÃO-walker, tenant-scoped).
    --      current_user_id='-' desliga a cláusula walker-self.
    -- =====================================================================

    -- T1.a — tenantA: vê só walkA; não vaza walkB/walkO (tenantB).
    PERFORM set_config('app.current_tenant',  v_tenantA, true);
    PERFORM set_config('app.current_user_id', '-',       true);
    SELECT COUNT(*) INTO v_count FROM walks WHERE id IN (v_walkA, v_walkB, v_walkO);
    IF v_count <> 1 THEN RAISE EXCEPTION 'FAIL T1.a: sessao tenantA deveria ver 1 (walkA), viu %', v_count; END IF;
    SELECT COUNT(*) INTO v_count FROM walks WHERE id IN (v_walkB, v_walkO);
    IF v_count <> 0 THEN RAISE EXCEPTION 'FAIL T1.a: sessao tenantA VAZOU % walk(s) do tenantB', v_count; END IF;
    RAISE NOTICE 'PASS T1.a: sessao tenantA (nao-walker) ve so walkA; nao vaza tenantB.';

    -- T1.b — tenantB: vê walkB+walkO; não vaza walkA (tenantA).
    PERFORM set_config('app.current_tenant',  v_tenantB, true);
    PERFORM set_config('app.current_user_id', '-',       true);
    SELECT COUNT(*) INTO v_count FROM walks WHERE id IN (v_walkA, v_walkB, v_walkO);
    IF v_count <> 2 THEN RAISE EXCEPTION 'FAIL T1.b: sessao tenantB deveria ver 2 (walkB+walkO), viu %', v_count; END IF;
    SELECT COUNT(*) INTO v_count FROM walks WHERE id = v_walkA;
    IF v_count <> 0 THEN RAISE EXCEPTION 'FAIL T1.b: sessao tenantB VAZOU walkA (tenantA)'; END IF;
    RAISE NOTICE 'PASS T1.b: sessao tenantB ve walkB+walkO; nao vaza walkA.';

    -- =====================================================================
    -- T2 — Cláusula walker-self (0049), defesa em profundidade sob tenant
    --      ESPECÍFICO: o passeador ve seus walks de OUTRO tenant, sem ver
    --      os de terceiros.
    -- =====================================================================

    -- T2.a — tenantA + walker W: ve walkA (tenant) + walkB (walker-self, tenantB), NAO walkO.
    PERFORM set_config('app.current_tenant',  v_tenantA, true);
    PERFORM set_config('app.current_user_id', v_walker,  true);
    SELECT COUNT(*) INTO v_count FROM walks WHERE id IN (v_walkA, v_walkB, v_walkO);
    IF v_count <> 2 THEN RAISE EXCEPTION 'FAIL T2.a: walker W sob tenantA deveria ver 2 (walkA + walkB via walker-self), viu %', v_count; END IF;
    SELECT COUNT(*) INTO v_count FROM walks WHERE id = v_walkB;
    IF v_count <> 1 THEN RAISE EXCEPTION 'FAIL T2.a: cláusula walker-self deveria expor walkB (tenantB) ao walker W sob tenantA'; END IF;
    SELECT COUNT(*) INTO v_count FROM walks WHERE id = v_walkO;
    IF v_count <> 0 THEN RAISE EXCEPTION 'FAIL T2.a: walker W NAO deveria ver walkO (de outro passeador)'; END IF;
    RAISE NOTICE 'PASS T2.a: walker-self expoe walkB cross-tenant ao W (sob tenantA) e esconde walkO.';

    -- =====================================================================
    -- T3 — Camada de aplicação: escopo '*' (RLS permissivo) + filtro walker_id
    --      da query. É o caminho real de /walker/walks.
    -- =====================================================================

    -- T3.doc — sob '*' o RLS e PERMISSIVO: sem filtro de app, "veem-se" os 3.
    --   Isso NAO e vazamento: a app SEMPRE adiciona o filtro walker_id (T3.a).
    PERFORM set_config('app.current_tenant',  '*',      true);
    PERFORM set_config('app.current_user_id', v_walker, true);
    SELECT COUNT(*) INTO v_count FROM walks WHERE id IN (v_walkA, v_walkB, v_walkO);
    IF v_count <> 3 THEN RAISE EXCEPTION 'FAIL T3.doc: sob ''*'' o RLS deveria ser permissivo (3 no nivel RLS), viu %', v_count; END IF;
    RAISE NOTICE 'NOTE T3.doc: sob ''*'' o RLS e permissivo (3 no nivel RLS) — a isolacao por passeador vem do filtro walker_id da app (T3.a/b).';

    -- T3.a — '*' + filtro de app de W: ve walkA+walkB, NAO walkO. (= /walker/walks de W)
    SELECT COUNT(*) INTO v_count FROM walks
     WHERE id IN (v_walkA, v_walkB, v_walkO)
       AND (walker_id = v_walker OR assigned_walker_id = v_walker);
    IF v_count <> 2 THEN RAISE EXCEPTION 'FAIL T3.a: /walker/walks de W deveria retornar 2 (walkA+walkB), viu %', v_count; END IF;
    SELECT COUNT(*) INTO v_count FROM walks
     WHERE id = v_walkO AND (walker_id = v_walker OR assigned_walker_id = v_walker);
    IF v_count <> 0 THEN RAISE EXCEPTION 'FAIL T3.a: /walker/walks de W NAO deveria conter walkO'; END IF;
    RAISE NOTICE 'PASS T3.a: app-layer (''*'' + filtro walker_id) — W ve walkA+walkB, nao walkO.';

    -- T3.b — '*' + filtro de app de O: ve só walkO.
    PERFORM set_config('app.current_user_id', v_other, true);
    SELECT COUNT(*) INTO v_count FROM walks
     WHERE id IN (v_walkA, v_walkB, v_walkO)
       AND (walker_id = v_other OR assigned_walker_id = v_other);
    IF v_count <> 1 THEN RAISE EXCEPTION 'FAIL T3.b: /walker/walks de O deveria retornar 1 (walkO), viu %', v_count; END IF;
    RAISE NOTICE 'PASS T3.b: app-layer — O ve so walkO.';

    RAISE NOTICE '=== TODAS AS ASSERCOES PASSARAM (T1 isolamento-tenant, T2 walker-self, T3 app-layer) ===';
END $$;

-- ---------------------------------------------------------------------------
-- SEÇÃO 3: ENCERRAMENTO — reset de role e rollback (nada persiste)
-- ---------------------------------------------------------------------------

RESET ROLE;

ROLLBACK;

-- Se chegou aqui sem EXCEPTION, todas as asserções passaram e o ROLLBACK
-- descartou os dados sintéticos. Se alguma EXCEPTION disparou no bloco DO, a
-- transação foi abortada e nada foi persistido — investigar a policy.
