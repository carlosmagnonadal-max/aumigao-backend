-- 0109_walk_emergency_calls — Botão de Emergência (S1). Equivalente SQL da migration Alembic 0109.
-- Aplicar no Neon SQL Editor (role dono neondb_owner) ANTES do deploy do backend.
-- Ordem: 0109 -> 0110_walker_training.sql -> 0111_local_rules_pet_reactive.sql.
-- Pré-condição: SELECT version_num FROM alembic_version;  -> '0108_user_apple_sub'
-- Idempotente: pode rodar de novo sem efeito (IF NOT EXISTS / DROP POLICY IF EXISTS).
-- NUNCA guarda telefone: o número do tutor só existe na resposta HTTP do acionamento.
BEGIN;

-- Trava de ordem: só segue se o banco estiver na 0108 (primeira vez) ou já na 0109+ (reexecução).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM alembic_version
         WHERE version_num IN ('0108_user_apple_sub', '0109_walk_emergency_calls',
                               '0110_walker_training', '0111_local_rules_pet_reactive')
    ) THEN
        RAISE EXCEPTION 'alembic_version inesperada (esperado 0108_user_apple_sub). Nada foi aplicado.';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS walk_emergency_calls (
    id               VARCHAR PRIMARY KEY,
    tenant_id        VARCHAR NULL REFERENCES tenants (id),
    walk_id          VARCHAR NOT NULL REFERENCES walks (id),
    walker_user_id   VARCHAR NOT NULL REFERENCES users (id),
    tutor_user_id    VARCHAR NULL REFERENCES users (id),
    reason           VARCHAR NULL,
    mode             VARCHAR NOT NULL,
    provider         VARCHAR NOT NULL DEFAULT 'none',
    provider_call_id VARCHAR NULL,
    status           VARCHAR NOT NULL,
    fallback_reason  TEXT NULL,
    created_at       TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    ended_at         TIMESTAMP WITHOUT TIME ZONE NULL
);
CREATE INDEX IF NOT EXISTS ix_walk_emergency_calls_tenant_id
    ON walk_emergency_calls (tenant_id);
CREATE INDEX IF NOT EXISTS ix_walk_emergency_calls_walk_created
    ON walk_emergency_calls (walk_id, created_at);

-- RLS por tenant: USING com NULL allowance, WITH CHECK estrito (só '*' grava NULL) — padrão 0045/0106.
ALTER TABLE walk_emergency_calls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON walk_emergency_calls;
CREATE POLICY tenant_isolation ON walk_emergency_calls
  USING ( current_setting('app.current_tenant', true) = '*'
          OR tenant_id IS NULL
          OR tenant_id::text = current_setting('app.current_tenant', true) )
  WITH CHECK ( current_setting('app.current_tenant', true) = '*'
               OR tenant_id::text = current_setting('app.current_tenant', true) );

-- O role da app (non-owner, sem BYPASSRLS) precisa de DML (padrão 0050/0053/0110).
GRANT SELECT, INSERT, UPDATE, DELETE ON walk_emergency_calls TO aumigao_app;

UPDATE alembic_version SET version_num = '0109_walk_emergency_calls'
 WHERE version_num = '0108_user_apple_sub';

COMMIT;

-- Conferência (rodar depois, fora da transação):
--   SELECT version_num FROM alembic_version;                                   -> 0109_walk_emergency_calls
--   SELECT relrowsecurity FROM pg_class WHERE relname = 'walk_emergency_calls'; -> true
--   SELECT policyname FROM pg_policies WHERE tablename = 'walk_emergency_calls'; -> tenant_isolation
--   SELECT has_table_privilege('aumigao_app', 'walk_emergency_calls', 'INSERT'); -> true
