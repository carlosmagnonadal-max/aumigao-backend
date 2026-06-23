-- 0049_walks_policy_walker_self.sql
-- Equivalente SQL da migration Alembic 0049_walks_policy_walker_self.
-- Cole e execute no Neon SQL Editor como dono (neondb_owner).
-- Idempotente: ALTER POLICY é seguro re-executar (substitui o USING em vigor).
--
-- Escopo: Fase 1 Passo 2 (Passo M2) — estende USING da policy tenant_isolation
-- em "walks" para incluir cláusula de walker-self (passeador vê seus próprios
-- walks de qualquer tenant com rls_tenant="*").
--
-- Invariante de segurança:
--   • WITH CHECK PERMANECE INALTERADO (escrita continua tenant-scoped).
--   • Apenas USING é estendido (leitura).
--   • A segunda barreira é o filtro walker_id==user.id na query da rota.
--
-- Referência: docs/multi-tenant-walker/ (PRD, SPEC, DECISÕES).
--             Pré-requisito: 0048_walker_multitenant.sql aplicado.

ALTER POLICY tenant_isolation ON walks
  USING (
    current_setting('app.current_tenant', true) = '*'
    OR tenant_id::text = current_setting('app.current_tenant', true)
    OR (
      current_setting('app.current_user_id', true) NOT IN ('-', '')
      AND (
        walker_id::text = current_setting('app.current_user_id', true)
        OR assigned_walker_id::text = current_setting('app.current_user_id', true)
      )
    )
  )
  WITH CHECK (
    current_setting('app.current_tenant', true) = '*'
    OR tenant_id::text = current_setting('app.current_tenant', true)
  );

-- ── Verificação pós-M2 (execute como comentário ou manualmente) ─────────────
--
-- 1. Tutor não vê linhas a mais:
--    SET LOCAL app.current_tenant = 'tenant-alpha';
--    SET LOCAL app.current_user_id = '-';
--    SELECT count(*) FROM walks;
--    -- Deve retornar apenas walks do tenant-alpha (mesma contagem que antes).
--
-- 2. Walker sob tenant X vê seus walks de tenant Y:
--    SET LOCAL app.current_tenant = '*';
--    SET LOCAL app.current_user_id = '<walker_uuid>';
--    SELECT count(*), tenant_id FROM walks
--    WHERE walker_id = '<walker_uuid>' OR assigned_walker_id = '<walker_uuid>'
--    GROUP BY tenant_id;
--    -- Deve mostrar walks de todos os tenants do passeador.
--
-- 3. Contagem por tenant do admin inalterada:
--    SET LOCAL app.current_tenant = 'tenant-alpha';
--    SET LOCAL app.current_user_id = '-';
--    SELECT count(*) FROM walks WHERE tenant_id = 'tenant-alpha';
--    -- Mesmo valor de antes da migration.
--
-- 4. Walker B não vê walks do walker A:
--    SET LOCAL app.current_tenant = '*';
--    SET LOCAL app.current_user_id = '<walker_b_uuid>';
--    SELECT count(*) FROM walks
--    WHERE walker_id = '<walker_a_uuid>' AND assigned_walker_id != '<walker_b_uuid>';
--    -- Deve retornar 0 (walker B não vê walks de A onde não é assigned).
