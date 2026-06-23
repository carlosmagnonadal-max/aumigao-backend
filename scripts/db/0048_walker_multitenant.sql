-- 0048_walker_multitenant.sql
-- Equivalente SQL da migration Alembic 0048_walker_multitenant.
-- Cole e execute no Neon SQL Editor (role dono / neondb_owner).
-- Todas as instruções são idempotentes (ADD COLUMN IF NOT EXISTS / IF NOT EXISTS).
--
-- Escopo: Fase 1 Passo 1 (Passo M1) — colunas + índice único parcial.
-- NÃO inclui ALTER POLICY walks (isso é Passo 2).
--
-- Referência: docs/multi-tenant-walker/ (PRD, SPEC, DECISÕES).

-- ── tenant_walker_access ────────────────────────────────────────────────────

ALTER TABLE tenant_walker_access
    ADD COLUMN IF NOT EXISTS commission_percent NUMERIC(5,2) NULL;

ALTER TABLE tenant_walker_access
    ADD COLUMN IF NOT EXISTS requirements_met BOOLEAN NOT NULL DEFAULT true;

ALTER TABLE tenant_walker_access
    ADD COLUMN IF NOT EXISTS initiated_by VARCHAR(16) NOT NULL DEFAULT 'tenant';

-- ── walker_network_profile ──────────────────────────────────────────────────

ALTER TABLE walker_network_profile
    ADD COLUMN IF NOT EXISTS exclusive_tenant_id VARCHAR NULL REFERENCES tenants(id);

-- ── tenants ─────────────────────────────────────────────────────────────────

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS network_access_override BOOLEAN NULL;

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS network_access_addon BOOLEAN NOT NULL DEFAULT false;

-- ── índice único parcial ────────────────────────────────────────────────────
-- Garante que um passeador só pode ter UM acesso do tipo 'tenant_exclusive'
-- com status 'active' por vez (regra de negócio: exclusividade).

CREATE UNIQUE INDEX IF NOT EXISTS uq_walker_one_active_exclusive
    ON tenant_walker_access(walker_user_id)
    WHERE status = 'active' AND access_type = 'tenant_exclusive';
