-- =============================================================================
-- verify_rls_availability_exceptions.sql
-- Verificação de RLS — policy walker_self_isolation (walker_availability_exceptions)
-- =============================================================================
--
-- PROPÓSITO
--   Provar, em PostgreSQL real (Neon) — o que o SQLite dos testes NÃO cobre —
--   que a policy walker_self_isolation de walker_availability_exceptions isola
--   corretamente, em 3 camadas:
--     T1 — RLS permissivo sob '*': escopo global vê TODAS as exceções (design intencional).
--          O isolamento real por passeador é feito pela camada de app (filtro walker_user_id).
--     T2 — Isolamento RLS puro sob tenant-scoped: tenant != '*' faz USING virer
--          `'x'='*' (false) OR walker_user_id=W` → filtro RLS real por passeador.
--     T3 — Camada de app: escopo GLOBAL '*' (RLS permissivo) + filtro walker_user_id
--          da query isola cada passeador, igual ao padrão de walks/Passo 3 (T3).
--
-- COMO RODAR (Neon SQL Editor, como o role dono usado em M1/M2)
--   Cole inteiro e execute. Sai uma TABELA de 7 linhas; todas devem dar status=PASS.
--   NÃO-DESTRUTIVO: tudo vive em BEGIN...ROLLBACK — nada é persistido (nem o GRANT).
--
-- ⚠️ NOTAS DE METODOLOGIA (aprendidas na execução de verify_rls_walker_self.sql, 2026-06-23)
--   1. O RLS só se aplica ao role da aplicação (aumigao_app: non-owner, SEM
--      BYPASSRLS). O role DONO do Neon faz BYPASS — rodar as asserções como dono
--      "vê tudo" e NÃO testa o RLS. `ALTER TABLE ... FORCE ROW LEVEL SECURITY`
--      NÃO resolve, pois BYPASSRLS/superuser tem precedência sobre FORCE.
--   2. Por isso as asserções rodam sob `SET ROLE aumigao_app`. Para o dono poder
--      fazer isso, damos membership TEMPORÁRIA (`GRANT aumigao_app TO CURRENT_USER`)
--      dentro da transação — revertida no ROLLBACK.
--   3. A temp table de resultados é criada DEPOIS do SET ROLE (dona = aumigao_app)
--      e lida ANTES do RESET ROLE, senão dá "permission denied for table".
--   4. A policy desta tabela (0050.sql) é:
--        USING  ( app.current_tenant='*' OR walker_user_id = app.current_user_id )
--        WITH CHECK ( walker_user_id = app.current_user_id )
--      Isso significa:
--        - Sob '*': RLS permissivo (primeiro termo true) — vê TODAS as exceções.
--          O isolamento por passeador vem do filtro `walker_user_id=X` na query da rota.
--        - Sob tenant-scoped (qualquer valor != '*'): primeiro termo false, só vê
--          as próprias exceções — isolamento REAL por RLS.
--      Esse comportamento é idêntico ao de walks (T3), e é testado em T1/T3 abaixo.
-- =============================================================================

ROLLBACK;
BEGIN;

-- Membership temporária para poder "vestir" o role do app (revertida no ROLLBACK).
GRANT aumigao_app TO CURRENT_USER;

-- ---------------------------------------------------------------------------
-- SETUP — dados sintéticos (prefixo rlsverify_) inseridos como dono (bypass RLS).
--   walkerW  e walkerO são dois passeadores distintos.
--   exW1, exW2 = exceções do W; exO = exceção do O.
-- ---------------------------------------------------------------------------
INSERT INTO users (id, email, password_hash, role, is_active, token_version, must_change_password, created_at) VALUES
    ('rlsverify_walkerW', 'rlsverify_walkerW@rlstest.invalid', '$2b$12$x', 'walker', true, 0, false, now()),
    ('rlsverify_walkerO', 'rlsverify_walkerO@rlstest.invalid', '$2b$12$x', 'walker', true, 0, false, now());

INSERT INTO walker_availability_exceptions (id, walker_user_id, exception_date, kind, created_at, updated_at) VALUES
    ('rlsverify_exW1', 'rlsverify_walkerW', '2099-07-01', 'block',    now(), now()),
    ('rlsverify_exW2', 'rlsverify_walkerW', '2099-07-02', 'block',    now(), now()),
    ('rlsverify_exO',  'rlsverify_walkerO', '2099-07-01', 'block',    now(), now());

-- ---------------------------------------------------------------------------
-- ASSERÇÕES — sob aumigao_app (RLS ATIVO). Temp table criada após o SET ROLE.
-- ---------------------------------------------------------------------------
SET ROLE aumigao_app;

CREATE TEMP TABLE rlsverify_out (n int, cenario text, esperado int, obtido int, status text);

DO $$
DECLARE v int;
  W  text := 'rlsverify_walkerW';
  O  text := 'rlsverify_walkerO';
  W1 text := 'rlsverify_exW1';
  W2 text := 'rlsverify_exW2';
  EO text := 'rlsverify_exO';
BEGIN
  -- -------------------------------------------------------------------------
  -- T1 — RLS permissivo sob '*': escopo global vê TODAS as exceções.
  --   Policy USING: 'app.current_tenant'='*' → TRUE (bypassa segundo termo).
  --   DESIGN INTENCIONAL: isolamento real vem do filtro de app (T3 abaixo).
  -- -------------------------------------------------------------------------
  PERFORM set_config('app.current_tenant', '*', true);
  PERFORM set_config('app.current_user_id', W, true);

  SELECT count(*) INTO v FROM walker_availability_exceptions WHERE id IN (W1, W2, EO);
  INSERT INTO rlsverify_out VALUES (1, 'T1 sob * RLS e permissivo (ve todas as 3 excecoes)', 3, v,
    CASE WHEN v = 3 THEN 'PASS' ELSE 'FAIL' END);

  -- -------------------------------------------------------------------------
  -- T2 — Isolamento RLS puro sob tenant-scoped (tenant != '*').
  --   Policy USING: 'x'='*' → FALSE, avalia segundo termo (walker_user_id=W).
  --   Resultado esperado: W vê só suas exceções; não vê a de O.
  -- -------------------------------------------------------------------------
  PERFORM set_config('app.current_tenant', 'rlsverify_tenantX', true);
  PERFORM set_config('app.current_user_id', W, true);

  SELECT count(*) INTO v FROM walker_availability_exceptions WHERE id IN (W1, W2, EO);
  INSERT INTO rlsverify_out VALUES (2, 'T2 W sob tenant-scoped ve so suas proprias excecoes (W1+W2)', 2, v,
    CASE WHEN v = 2 THEN 'PASS' ELSE 'FAIL' END);

  SELECT count(*) INTO v FROM walker_availability_exceptions WHERE id = EO;
  INSERT INTO rlsverify_out VALUES (3, 'T2 W sob tenant-scoped NAO ve excecao de O', 0, v,
    CASE WHEN v = 0 THEN 'PASS' ELSE 'FAIL' END);

  PERFORM set_config('app.current_user_id', O, true);

  SELECT count(*) INTO v FROM walker_availability_exceptions WHERE id IN (W1, W2, EO);
  INSERT INTO rlsverify_out VALUES (4, 'T2 O sob tenant-scoped ve so sua propria excecao (EO)', 1, v,
    CASE WHEN v = 1 THEN 'PASS' ELSE 'FAIL' END);

  SELECT count(*) INTO v FROM walker_availability_exceptions WHERE id IN (W1, W2);
  INSERT INTO rlsverify_out VALUES (5, 'T2 O sob tenant-scoped NAO ve excecoes de W', 0, v,
    CASE WHEN v = 0 THEN 'PASS' ELSE 'FAIL' END);

  -- -------------------------------------------------------------------------
  -- T3 — Camada de app: '*' permissivo + filtro walker_user_id da query.
  --   Simula o que a rota GET /walker/availability/exceptions faz na prática.
  -- -------------------------------------------------------------------------
  PERFORM set_config('app.current_tenant', '*', true);
  PERFORM set_config('app.current_user_id', W, true);

  SELECT count(*) INTO v FROM walker_availability_exceptions
    WHERE id IN (W1, W2, EO) AND walker_user_id = W;
  INSERT INTO rlsverify_out VALUES (6, 'T3 /walker/availability/exceptions de W retorna so W1+W2', 2, v,
    CASE WHEN v = 2 THEN 'PASS' ELSE 'FAIL' END);

  PERFORM set_config('app.current_user_id', O, true);

  SELECT count(*) INTO v FROM walker_availability_exceptions
    WHERE id IN (W1, W2, EO) AND walker_user_id = O;
  INSERT INTO rlsverify_out VALUES (7, 'T3 /walker/availability/exceptions de O retorna so EO', 1, v,
    CASE WHEN v = 1 THEN 'PASS' ELSE 'FAIL' END);

END $$;

SELECT n AS "#", cenario, esperado, obtido, status FROM rlsverify_out ORDER BY n;

RESET ROLE;
ROLLBACK;
