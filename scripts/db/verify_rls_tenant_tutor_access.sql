-- =============================================================================
-- verify_rls_tenant_tutor_access.sql
-- Verificação de RLS — policy tenant_tutor_access_isolation (migration 0053)
-- =============================================================================
--
-- PROPÓSITO
--   Provar, em PostgreSQL real (Neon) — o que o SQLite dos testes NÃO cobre —
--   que a policy tenant_tutor_access_isolation isola corretamente, em 3 camadas:
--     T1 — Isolamento de tenant: sessão tenant-scoped vê só os próprios registros.
--     T2 — Escopo global '*': sessão com '*' vê registros de todos os tenants.
--     T3 — WITH CHECK: escrita cross-tenant é barrada pela policy.
--
-- COMO RODAR (Neon SQL Editor, como o role dono neondb_owner)
--   Cole inteiro e execute. Sai uma TABELA de 3 linhas; todas devem dar ok=true.
--   NÃO-DESTRUTIVO: tudo vive em BEGIN...ROLLBACK — nada é persistido.
--
-- ⚠️ NOTAS DE METODOLOGIA (herdadas de verify_rls_walker_self.sql, 2026-06-23)
--   1. RLS só se aplica ao role da aplicação (aumigao_app: non-owner, SEM
--      BYPASSRLS). O role DONO faz BYPASS — asserções devem rodar como aumigao_app.
--   2. GRANT aumigao_app TO CURRENT_USER dá membership temporária (revertida no ROLLBACK).
--   3. A temp table é criada como aumigao_app (após SET ROLE) e lida antes do RESET ROLE.
--   4. tenants/users têm colunas NOT NULL sem server_default — seeds passam valores
--      explícitos (status, plan, network_access_addon, password_hash, etc.).
--
-- =============================================================================

ROLLBACK;
BEGIN;

-- Membership temporária para poder "vestir" o role do app (revertida no ROLLBACK).
GRANT aumigao_app TO CURRENT_USER;

-- ---------------------------------------------------------------------------
-- SETUP — dados sintéticos (prefixo rlsverify_) inseridos como dono (bypass RLS).
-- ---------------------------------------------------------------------------
INSERT INTO tenants (id, name, slug, status, plan, network_access_addon, created_at, updated_at) VALUES
    ('rlsverify_tA', 'RLS Tutor A', 'rlsverify-tutor-a', 'active', 'starter', false, now(), now()),
    ('rlsverify_tB', 'RLS Tutor B', 'rlsverify-tutor-b', 'active', 'starter', false, now(), now())
    ON CONFLICT DO NOTHING;

INSERT INTO users (id, email, password_hash, tenant_id, full_name, role, is_active, token_version, must_change_password, created_at) VALUES
    ('rlsverify_tutor1', 'rlsverify_tutor1@rlstest.invalid', '$2b$12$x', 'rlsverify_tA', 'Tutor 1', 'tutor', true, 0, false, now())
    ON CONFLICT DO NOTHING;

INSERT INTO tenant_tutor_access (id, tenant_id, tutor_user_id, status, initiated_by, created_at, updated_at) VALUES
    ('rlsverify_tta_A', 'rlsverify_tA', 'rlsverify_tutor1', 'active', 'tutor', now(), now()),
    ('rlsverify_tta_B', 'rlsverify_tB', 'rlsverify_tutor1', 'active', 'tutor', now(), now())
    ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- ASSERÇÕES — sob aumigao_app (RLS ATIVO). Temp table criada após o SET ROLE.
-- ---------------------------------------------------------------------------
SET ROLE aumigao_app;

CREATE TEMP TABLE rlsverify_out (n int, label text, ok boolean) ON COMMIT DROP;

-- T1 — isolamento de tenant: sessão tenantA vê só o seu registro.
SET LOCAL app.current_tenant = 'rlsverify_tA';
INSERT INTO rlsverify_out
    SELECT 1, 'T1 tenant A isola (ve so o proprio)',
        (SELECT count(*) FROM tenant_tutor_access WHERE id LIKE 'rlsverify_%') = 1;

-- T2 — escopo global '*': vê os dois registros.
SET LOCAL app.current_tenant = '*';
INSERT INTO rlsverify_out
    SELECT 2, 'T2 escopo global * ve os 2 registros',
        (SELECT count(*) FROM tenant_tutor_access WHERE id LIKE 'rlsverify_%') = 2;

-- T3 — WITH CHECK barra escrita cross-tenant.
SET LOCAL app.current_tenant = 'rlsverify_tA';
DO $$ BEGIN
    BEGIN
        INSERT INTO tenant_tutor_access (id, tenant_id, tutor_user_id, status, initiated_by, created_at, updated_at)
        VALUES ('rlsverify_x', 'rlsverify_tB', 'rlsverify_tutor1', 'active', 'tutor', now(), now());
        INSERT INTO rlsverify_out VALUES (3, 'T3 WITH CHECK barra cross-tenant', false);
    EXCEPTION WHEN insufficient_privilege OR check_violation THEN
        INSERT INTO rlsverify_out VALUES (3, 'T3 WITH CHECK barra cross-tenant', true);
    END;
END $$;

SELECT n, label, ok FROM rlsverify_out ORDER BY n;

RESET ROLE;
ROLLBACK;
