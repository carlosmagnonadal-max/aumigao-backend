-- =============================================================================
-- verify_rls_walker_self.sql
-- Verificação de RLS — policy tenant_isolation (walks) + cláusula walker-self (0049)
-- =============================================================================
--
-- PROPÓSITO
--   Provar, em PostgreSQL real (Neon) — o que o SQLite dos testes NÃO cobre —
--   que a policy tenant_isolation de walks isola corretamente, em 3 camadas:
--     T1 — Isolamento de tenant: sessão tenant-scoped (não-walker) não vê outro tenant.
--     T2 — Cláusula walker-self (0049): sob um tenant específico, o passeador vê
--          seus walks de OUTRO tenant (cross-tenant) sem ver os de terceiros.
--     T3 — Camada de app: escopo GLOBAL '*' (RLS permissivo de propósito) + o
--          filtro walker_id da query da rota isola por passeador.
--
-- COMO RODAR (Neon SQL Editor, como o role dono usado em M1/M2)
--   Cole inteiro e execute. Sai uma TABELA de 9 linhas; todas devem dar status=PASS.
--   NÃO-DESTRUTIVO: tudo vive em BEGIN...ROLLBACK — nada é persistido (nem o GRANT).
--
-- ⚠️ NOTAS DE METODOLOGIA (aprendidas na 1ª execução, 2026-06-23)
--   1. O RLS só se aplica ao role da aplicação (aumigao_app: non-owner, SEM
--      BYPASSRLS). O role DONO do Neon faz BYPASS — rodar as asserções como dono
--      "vê tudo" e NÃO testa o RLS. `ALTER TABLE ... FORCE ROW LEVEL SECURITY`
--      NÃO resolve, pois BYPASSRLS/superuser tem precedência sobre FORCE.
--   2. Por isso as asserções rodam sob `SET ROLE aumigao_app`. Para o dono poder
--      fazer isso, damos membership TEMPORÁRIA (`GRANT aumigao_app TO CURRENT_USER`)
--      dentro da transação — revertida no ROLLBACK.
--   3. A temp table de resultados é criada DEPOIS do SET ROLE (dona = aumigao_app)
--      e lida ANTES do RESET ROLE, senão dá "permission denied for table".
--   4. tenants/tenant_walker_access têm created_at/updated_at NOT NULL sem
--      server_default — precisam de valor explícito em INSERT cru.
--
-- RESULTADO VALIDADO EM PROD: 2026-06-23 — 9/9 PASS.
-- =============================================================================

ROLLBACK;
BEGIN;

-- Membership temporária para poder "vestir" o role do app (revertida no ROLLBACK).
GRANT aumigao_app TO CURRENT_USER;

-- ---------------------------------------------------------------------------
-- SETUP — dados sintéticos (prefixo rlsverify_) inseridos como dono (bypass RLS).
--   walkA=(tenantA,W)  walkB=(tenantB,W)  walkO=(tenantB,O)
-- ---------------------------------------------------------------------------
INSERT INTO tenants (id,name,slug,status,plan,network_access_addon,created_at,updated_at) VALUES
    ('rlsverify_tenantA','RLS Verify Tenant A','rlsverify-a','active','starter',false,now(),now()),
    ('rlsverify_tenantB','RLS Verify Tenant B','rlsverify-b','active','starter',false,now(),now());
INSERT INTO users (id,email,password_hash,tenant_id,full_name,role,is_active,token_version,must_change_password,created_at) VALUES
    ('rlsverify_tutor','rlsverify_tutor@rlstest.invalid','$2b$12$x','rlsverify_tenantA','Tutor','tutor',true,0,false,now()),
    ('rlsverify_walkerW','rlsverify_walkerW@rlstest.invalid','$2b$12$x',NULL,'Walker W','walker',true,0,false,now()),
    ('rlsverify_walkerO','rlsverify_walkerO@rlstest.invalid','$2b$12$x',NULL,'Walker O','walker',true,0,false,now());
INSERT INTO pets (id,tutor_id,tenant_id,name,species,sex,breed,size,behavior_notes,allergies,medications,restrictions,health_notes,is_social,afraid_of_noise,pulls_leash,can_walk_with_other_pets,is_neutered,created_at) VALUES
    ('rlsverify_pet','rlsverify_tutor','rlsverify_tenantA','Pet','Cachorro','','','','','','','','',true,false,false,false,false,now());
INSERT INTO tenant_walker_access (id,tenant_id,walker_user_id,access_type,status,requirements_met,initiated_by,created_at,updated_at) VALUES
    ('rlsverify_twa_WA','rlsverify_tenantA','rlsverify_walkerW','shared_network','active',true,'tenant',now(),now()),
    ('rlsverify_twa_WB','rlsverify_tenantB','rlsverify_walkerW','shared_network','active',true,'tenant',now(),now()),
    ('rlsverify_twa_OB','rlsverify_tenantB','rlsverify_walkerO','shared_network','active',true,'tenant',now(),now());
INSERT INTO walks (id,tutor_id,tenant_id,walker_id,assigned_walker_id,pet_id,scheduled_date,duration_minutes,price,status,pickup_method,modality,destination,address_snapshot,notes,created_at,operational_status,walker_selection_mode,current_attempt,max_attempts) VALUES
    ('rlsverify_walkA','rlsverify_tutor','rlsverify_tenantA','rlsverify_walkerW','rlsverify_walkerW','rlsverify_pet','2099-01-01',30,50,'Agendado','Buscar em casa','standard','','{}','',now(),'ride_scheduled','auto',0,3),
    ('rlsverify_walkB','rlsverify_tutor','rlsverify_tenantB','rlsverify_walkerW','rlsverify_walkerW','rlsverify_pet','2099-01-02',30,50,'Agendado','Buscar em casa','standard','','{}','',now(),'ride_scheduled','auto',0,3),
    ('rlsverify_walkO','rlsverify_tutor','rlsverify_tenantB','rlsverify_walkerO','rlsverify_walkerO','rlsverify_pet','2099-01-03',30,50,'Agendado','Buscar em casa','standard','','{}','',now(),'ride_scheduled','auto',0,3);

-- ---------------------------------------------------------------------------
-- ASSERÇÕES — sob aumigao_app (RLS ATIVO). Temp table criada após o SET ROLE.
-- ---------------------------------------------------------------------------
SET ROLE aumigao_app;

CREATE TEMP TABLE rlsverify_out (n int, cenario text, esperado int, obtido int, status text);

DO $$
DECLARE v int;
  W text:='rlsverify_walkerW'; O text:='rlsverify_walkerO';
  A text:='rlsverify_walkA'; B text:='rlsverify_walkB'; X text:='rlsverify_walkO';
  tA text:='rlsverify_tenantA'; tB text:='rlsverify_tenantB';
BEGIN
  -- T1 — isolamento de tenant (não-walker)
  PERFORM set_config('app.current_tenant',tA,true); PERFORM set_config('app.current_user_id','-',true);
  SELECT count(*) INTO v FROM walks WHERE id IN (A,B,X);
  INSERT INTO rlsverify_out VALUES (1,'T1.a tenantA (nao-walker) ve so walkA',1,v,CASE WHEN v=1 THEN 'PASS' ELSE 'FAIL' END);
  SELECT count(*) INTO v FROM walks WHERE id IN (B,X);
  INSERT INTO rlsverify_out VALUES (2,'T1.a tenantA NAO vaza walks do tenantB',0,v,CASE WHEN v=0 THEN 'PASS' ELSE 'FAIL' END);

  PERFORM set_config('app.current_tenant',tB,true); PERFORM set_config('app.current_user_id','-',true);
  SELECT count(*) INTO v FROM walks WHERE id IN (A,B,X);
  INSERT INTO rlsverify_out VALUES (3,'T1.b tenantB ve walkB+walkO',2,v,CASE WHEN v=2 THEN 'PASS' ELSE 'FAIL' END);
  SELECT count(*) INTO v FROM walks WHERE id=A;
  INSERT INTO rlsverify_out VALUES (4,'T1.b tenantB NAO vaza walkA',0,v,CASE WHEN v=0 THEN 'PASS' ELSE 'FAIL' END);

  -- T2 — cláusula walker-self (0049) sob tenant específico
  PERFORM set_config('app.current_tenant',tA,true); PERFORM set_config('app.current_user_id',W,true);
  SELECT count(*) INTO v FROM walks WHERE id IN (A,B,X);
  INSERT INTO rlsverify_out VALUES (5,'T2 walker W sob tenantA ve walkA+walkB (cross-tenant proprio)',2,v,CASE WHEN v=2 THEN 'PASS' ELSE 'FAIL' END);
  SELECT count(*) INTO v FROM walks WHERE id=X;
  INSERT INTO rlsverify_out VALUES (6,'T2 walker W NAO ve walkO (de outro passeador)',0,v,CASE WHEN v=0 THEN 'PASS' ELSE 'FAIL' END);

  -- T3 — camada de app: '*' permissivo + filtro walker_id da query
  PERFORM set_config('app.current_tenant','*',true); PERFORM set_config('app.current_user_id',W,true);
  SELECT count(*) INTO v FROM walks WHERE id IN (A,B,X);
  INSERT INTO rlsverify_out VALUES (7,'T3 sob * o RLS e permissivo (a app filtra depois)',3,v,CASE WHEN v=3 THEN 'PASS' ELSE 'FAIL' END);
  SELECT count(*) INTO v FROM walks WHERE id IN (A,B,X) AND (walker_id=W OR assigned_walker_id=W);
  INSERT INTO rlsverify_out VALUES (8,'T3 /walker/walks de W = walkA+walkB',2,v,CASE WHEN v=2 THEN 'PASS' ELSE 'FAIL' END);

  PERFORM set_config('app.current_user_id',O,true);
  SELECT count(*) INTO v FROM walks WHERE id IN (A,B,X) AND (walker_id=O OR assigned_walker_id=O);
  INSERT INTO rlsverify_out VALUES (9,'T3 /walker/walks de O = so walkO',1,v,CASE WHEN v=1 THEN 'PASS' ELSE 'FAIL' END);
END $$;

SELECT n AS "#", cenario, esperado, obtido, status FROM rlsverify_out ORDER BY n;

RESET ROLE;
ROLLBACK;
