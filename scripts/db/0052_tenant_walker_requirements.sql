-- 0052_tenant_walker_requirements.sql — espelho da migration 0052 (F3.2).
-- Rodar no Neon SQL Editor (dono) ANTES do deploy. Idempotente.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS walker_extra_requirements JSONB;
ALTER TABLE tenant_walker_access ADD COLUMN IF NOT EXISTS requirements_submitted_at TIMESTAMP;
