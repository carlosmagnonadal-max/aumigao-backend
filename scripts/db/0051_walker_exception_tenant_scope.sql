-- 0051_walker_exception_tenant_scope.sql
-- Espelho SQL da migration 0051. Rodar no Neon SQL Editor (dono) ANTES do deploy. Idempotente.
ALTER TABLE walker_availability_exceptions
  ADD COLUMN IF NOT EXISTS tenant_id VARCHAR REFERENCES tenants(id);
CREATE INDEX IF NOT EXISTS ix_wae_tenant_id ON walker_availability_exceptions (tenant_id);
-- RLS walker-self da 0050 permanece válida (tenant_id não é fronteira de segurança).
